//! The engine compiled to wasm for the replay viewer (SPEC §8) and for games
//! played in the browser (SPEC §5).
//!
//! This exposes a small C ABI instead of using wasm-bindgen, so building it
//! needs only cargo and the `wasm32-unknown-unknown` target. Strings cross
//! the boundary as UTF-8 in wasm memory: JavaScript copies inputs into
//! buffers from `bb_alloc` and reads outputs at `bb_out_ptr`/`bb_out_len`.

use bitbath_engine::mapgen::{self, GENERATOR_VERSION};
use bitbath_engine::{Game, Rules, ENGINE_VERSION, PROTOCOL_VERSION};
use std::cell::RefCell;
use std::fmt::Write as _;

enum Step {
    /// Start a turn for this player.
    Turn(u8),
    Order(String),
}

struct Viewer {
    steps: Vec<Step>,
    /// The game before each turn-start step, as (step index, game).
    snapshots: Vec<(usize, Game)>,
    /// The game after every step, where a live game adds its next one.
    tip: Game,
    /// Whether the last turn is still being played. A replay's last turn is
    /// over; a live game's is open until `bb_end` ends it.
    open: bool,
}

thread_local! {
    static VIEWER: RefCell<Option<Viewer>> = const { RefCell::new(None) };
    static OUT: RefCell<String> = const { RefCell::new(String::new()) };
}

fn set_out(s: String) {
    OUT.with(|o| *o.borrow_mut() = s);
}

/// Reads an input string that JavaScript wrote into wasm memory.
///
/// # Safety
/// `ptr` must point to `len` readable bytes.
unsafe fn input(ptr: *const u8, len: usize) -> String {
    String::from_utf8_lossy(std::slice::from_raw_parts(ptr, len)).into_owned()
}

/// Allocates `len` bytes for JavaScript to write an input into.
#[no_mangle]
pub extern "C" fn bb_alloc(len: usize) -> *mut u8 {
    let mut buf = Vec::<u8>::with_capacity(len.max(1));
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr
}

/// Frees a buffer from `bb_alloc`.
///
/// # Safety
/// `ptr` and `len` must come from one `bb_alloc` call.
#[no_mangle]
pub unsafe extern "C" fn bb_free(ptr: *mut u8, len: usize) {
    drop(Vec::from_raw_parts(ptr, 0, len.max(1)));
}

#[no_mangle]
pub extern "C" fn bb_out_ptr() -> *const u8 {
    OUT.with(|o| o.borrow().as_ptr())
}

#[no_mangle]
pub extern "C" fn bb_out_len() -> usize {
    OUT.with(|o| o.borrow().len())
}

/// Loads a game. `setup` holds the replay header's setup lines. `script`
/// holds one step per line: `T <player>` starts a turn and `O <order>` runs
/// an order. Returns the number of steps and outputs the map as JSON, or
/// returns -1 and outputs an error message.
///
/// # Safety
/// Both pointers must point to the given number of readable bytes.
#[no_mangle]
pub unsafe extern "C" fn bb_load(
    setup_ptr: *const u8,
    setup_len: usize,
    script_ptr: *const u8,
    script_len: usize,
    open_turn: u32,
) -> i32 {
    let setup = input(setup_ptr, setup_len);
    let script = input(script_ptr, script_len);
    match load(&setup, &script, open_turn != 0) {
        Ok((viewer, info)) => {
            let n = viewer.steps.len() as i32;
            VIEWER.with(|v| *v.borrow_mut() = Some(viewer));
            set_out(info);
            n
        }
        Err(e) => {
            set_out(e);
            -1
        }
    }
}

/// Outputs the state after the first `step` steps as JSON. Returns 0, or -1
/// if no game is loaded.
#[no_mangle]
pub extern "C" fn bb_seek(step: u32) -> i32 {
    VIEWER.with(|v| match v.borrow().as_ref() {
        Some(viewer) => {
            set_out(viewer.state_json(step as usize));
            0
        }
        None => -1,
    })
}

/// Outputs the setup lines for a new game on a generated map (see
/// `new_setup`). Returns 0, or -1 with an error message as the output.
///
/// # Safety
/// `ptr` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn bb_setup(ptr: *const u8, len: usize) -> i32 {
    match new_setup(&input(ptr, len)) {
        Ok(setup) => {
            set_out(setup);
            0
        }
        Err(e) => {
            set_out(e);
            -1
        }
    }
}

/// Outputs the engine, protocol, and generator versions as JSON, for the
/// header of a replay recorded in the browser.
#[no_mangle]
pub extern "C" fn bb_versions() {
    set_out(format!(
        "{{\"bitbath\":{},\"protocol\":{PROTOCOL_VERSION},\"generator\":{GENERATOR_VERSION}}}",
        esc(ENGINE_VERSION)
    ));
}

/// Runs `f` on the loaded game. Returns -1 with an error message as the
/// output if no game is loaded or `f` fails.
fn with_viewer(f: impl FnOnce(&mut Viewer) -> Result<i32, String>) -> i32 {
    let result = VIEWER.with(|v| match v.borrow_mut().as_mut() {
        Some(viewer) => f(viewer),
        None => Err("no game is loaded".into()),
    });
    result.unwrap_or_else(|e| {
        set_out(e);
        -1
    })
}

/// Adds one step, written as in `bb_load`'s script, to the end of the loaded
/// game: this is how a live game is played. Outputs the step's reply lines
/// and returns 0, or returns -1 with an error message as the output.
///
/// # Safety
/// `ptr` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn bb_push(ptr: *const u8, len: usize) -> i32 {
    let line = input(ptr, len);
    with_viewer(|viewer| {
        let step = parse_step(&line)?.ok_or("empty step")?;
        set_out(viewer.push(step).join("\n"));
        Ok(0)
    })
}

/// Ends the turn being played in a live game. Returns 1 if that ended the
/// game, 0 if not, and -1 if no game is loaded.
#[no_mangle]
pub extern "C" fn bb_end() -> i32 {
    with_viewer(|viewer| Ok(viewer.end() as i32))
}

/// Outputs the init message for `player` (SPEC §6.2). Returns 0, or -1 if no
/// game is loaded.
#[no_mangle]
pub extern "C" fn bb_init_message(player: u32) -> i32 {
    with_viewer(|viewer| {
        if player != 1 && player != 2 {
            return Err(format!("bad player {player}"));
        }
        set_out(viewer.tip.init_message(player as u8).join("\n"));
        Ok(0)
    })
}

/// Outputs the turn message (SPEC §6.3) for the player whose turn it is in a
/// live game: what that player knows, and nothing more. Returns 0, or -1 if
/// no turn is being played.
#[no_mangle]
pub extern "C" fn bb_turn_message(time_ms: u32, bank_ms: u32) -> i32 {
    with_viewer(|viewer| {
        if viewer.tip.player == 0 || !viewer.open {
            return Err("no turn is being played".into());
        }
        set_out(viewer.tip.turn_message(time_ms as u64, bank_ms as u64).join("\n"));
        Ok(0)
    })
}

fn apply(game: &mut Game, step: &Step) -> Vec<String> {
    match step {
        Step::Turn(player) => {
            if game.player != 0 {
                game.end_turn();
            }
            if game.outcome.is_none() {
                game.begin_turn(*player);
            }
            Vec::new()
        }
        Step::Order(order) => game.apply(order),
    }
}

/// Parses one line of a script. Empty lines hold no step.
fn parse_step(line: &str) -> Result<Option<Step>, String> {
    if let Some(p) = line.strip_prefix("T ") {
        let player = p.trim().parse().map_err(|_| format!("bad step '{line}'"))?;
        if player != 1 && player != 2 {
            return Err(format!("bad player in '{line}'"));
        }
        Ok(Some(Step::Turn(player)))
    } else if let Some(order) = line.strip_prefix("O ") {
        Ok(Some(Step::Order(order.to_string())))
    } else if line.trim().is_empty() {
        Ok(None)
    } else {
        Err(format!("bad step '{line}'"))
    }
}

/// Rules and a generated map for a new game, as setup lines (SPEC §6.2) with
/// the `START` line. `config` holds `key = value` lines as in the files in
/// configs/, plus `seed` for the map generator.
fn new_setup(config: &str) -> Result<String, String> {
    let mut rules = Rules::default();
    let mut seed = 0u64;
    for line in config.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        if line.is_empty() {
            continue;
        }
        let (key, value) = line
            .split_once('=')
            .ok_or_else(|| format!("'{line}' isn't key = value"))?;
        let (key, value) = (key.trim(), value.trim().trim_matches('"'));
        if key == "seed" {
            seed = value.parse().map_err(|_| format!("seed: '{value}' is not a number"))?;
        } else {
            rules.set(key, value)?;
        }
    }
    rules.validate()?;
    let map = mapgen::generate(&rules, seed)?;
    Ok(Game::new(rules, map).setup_lines(true).join("\n"))
}

fn load(setup: &str, script: &str, open_turn: bool) -> Result<(Viewer, String), String> {
    let game = Game::from_setup(setup.lines())?;
    let mut steps = Vec::new();
    for line in script.lines() {
        steps.extend(parse_step(line)?);
    }
    let mut info = info_json(&game);
    let mut snapshots = vec![(0, game.clone())];
    // Each step's result line (empty for turn starts), so the viewer can
    // list a turn's orders with their outcomes without seeking to each.
    let mut results = Vec::with_capacity(steps.len());
    // Each player's army value at the start and after each turn, for the
    // viewer's strength chart.
    let mut army = vec![army_json(&game)];
    let mut g = game;
    for (i, step) in steps.iter().enumerate() {
        if i > 0 && matches!(step, Step::Turn(_)) {
            snapshots.push((i, g.clone()));
            army.push(army_json(&g));
        }
        let reply = apply(&mut g, step);
        results.push(esc(reply.last().map_or("", String::as_str)));
    }
    army.push(army_json(&g));
    info.pop();
    let _ = write!(
        info,
        ",\"results\":[{}],\"army\":[{}]}}",
        results.join(","),
        army.join(",")
    );
    let viewer = Viewer {
        steps,
        snapshots,
        tip: g,
        open: open_turn,
    };
    Ok((viewer, info))
}

fn army_json(game: &Game) -> String {
    format!("[{},{}]", game.army_value(1), game.army_value(2))
}

fn esc(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn info_json(game: &Game) -> String {
    let mut s = format!(
        "{{\"width\":{},\"height\":{},\"rows\":[",
        game.map.width, game.map.height
    );
    let rows: Vec<String> = game
        .map
        .tiles
        .chunks(game.map.width as usize)
        .map(|row| {
            esc(&row
                .iter()
                .map(|&t| game.rules.terrains[t as usize].ch)
                .collect::<String>())
        })
        .collect();
    s.push_str(&rows.join(","));
    s.push_str("],\"terrain\":{");
    let terrains: Vec<String> = game
        .rules
        .terrains
        .iter()
        .map(|t| {
            format!(
                "{}:{{\"name\":{},\"move_cost\":{},\"defense\":{},\"vision_bonus\":{},\"conceal\":{},\"naval_move_cost\":{}}}",
                esc(&t.ch.to_string()),
                esc(&t.name),
                t.move_cost,
                t.defense,
                t.vision_bonus,
                t.conceal,
                t.naval_move_cost
            )
        })
        .collect();
    s.push_str(&terrains.join(","));
    s.push_str("},\"units\":{");
    let units: Vec<String> = game
        .rules
        .units
        .iter()
        .map(|u| {
            format!(
                "{}:{{\"name\":{},\"cost\":{},\"max_hp\":{},\"move\":{},\"vision\":{},\"range\":[{},{}],\"flags\":{}}}",
                esc(&u.code.to_string()),
                esc(&u.name),
                u.cost,
                u.max_hp,
                u.mv,
                u.vision,
                u.range_min,
                u.range_max,
                esc(&u.flags())
            )
        })
        .collect();
    s.push_str(&units.join(","));
    s.push_str("},\"cities\":[");
    let cities: Vec<String> = game
        .map
        .cities
        .iter()
        .map(|c| format!("[{},{},{}]", c.hex.q, c.hex.r, c.owner))
        .collect();
    s.push_str(&cities.join(","));
    // Sites, with supply on (STRATEGIC.md §4): q, r, owner, output, cap. Sites
    // have no owner, so it's always 0, kept so the page reads the same shape.
    s.push_str("],\"sites\":[");
    let sites: Vec<String> = game
        .map
        .sites
        .iter()
        .map(|c| format!("[{},{},0,{},{}]", c.hex.q, c.hex.r, c.output, c.cap))
        .collect();
    s.push_str(&sites.join(","));
    let _ = write!(
        s,
        "],\"supply\":{},\"capture_points\":{}}}",
        game.rules.c.supply, game.rules.c.capture_points
    );
    s
}

impl Viewer {
    /// Adds a step to a live game and returns its reply lines.
    fn push(&mut self, step: Step) -> Vec<String> {
        if matches!(step, Step::Turn(_)) && !self.steps.is_empty() {
            self.snapshots.push((self.steps.len(), self.tip.clone()));
        }
        let reply = apply(&mut self.tip, &step);
        self.steps.push(step);
        self.open = true;
        reply
    }

    /// Ends the turn being played in a live game. Returns whether that
    /// ended the game.
    fn end(&mut self) -> bool {
        if self.tip.player != 0 {
            self.tip.end_turn();
        }
        self.open = false;
        self.tip.outcome.is_some()
    }

    fn state_json(&self, step: usize) -> String {
        let step = step.min(self.steps.len());
        // Resume from the last snapshot strictly before `step`, so the reply
        // of the step just taken is known.
        let at = self.snapshots.partition_point(|(i, _)| *i < step).saturating_sub(1);
        let (start, snapshot) = &self.snapshots[at];
        let mut game = snapshot.clone();
        let mut reply = Vec::new();
        for s in &self.steps[*start..step] {
            reply = apply(&mut game, s);
        }
        // No turn step follows the last turn to end it, so end it here. That
        // lets the round limit decide the game, as it did when it was played.
        if step == self.steps.len() && !self.open && game.player != 0 && game.outcome.is_none() {
            game.end_turn();
        }

        let mut out = format!(
            "{{\"step\":{step},\"round\":{},\"player\":{},\"gold\":[{},{}],\"units\":[",
            game.round, game.player, game.gold[1], game.gold[2]
        );
        // With supply on, units also carry wheat, bread, rations, and the
        // transport they're aboard, cities their bread, and sites their owner,
        // capture, and wheat. Sites have no owner or capture, so those are 0.
        let supply = game.rules.supply();
        let units: Vec<String> = game
            .units
            .iter()
            .map(|u| {
                let flags = u.acted as u8 | (u.moved as u8) << 1 | (u.fresh as u8) << 2;
                let food = if supply {
                    format!(",{},{},{},{}", u.wheat, u.bread, u.rations, u.carrier)
                } else {
                    String::new()
                };
                format!(
                    "[{},{},{},{},{},{},{},{},{}{food}]",
                    u.id,
                    u.owner,
                    esc(&game.rules.units[u.ty].code.to_string()),
                    u.hex.q,
                    u.hex.r,
                    u.hp,
                    u.mp,
                    flags,
                    u.fort
                )
            })
            .collect();
        out.push_str(&units.join(","));
        out.push_str("],\"cities\":[");
        let cities: Vec<String> = game
            .cities
            .iter()
            .map(|c| {
                let bread = if supply { format!(",{}", c.bread) } else { String::new() };
                format!("[{},{},{},{}{bread}]", c.owner, c.capturer, c.progress, c.built as u8)
            })
            .collect();
        out.push_str(&cities.join(","));
        if supply {
            let sites: Vec<String> = game.sites.iter().map(|s| format!("[0,0,0,{}]", s.wheat)).collect();
            let _ = write!(
                out,
                "],\"bread\":[{},{}],\"sites\":[{}",
                game.bread_of(1),
                game.bread_of(2),
                sites.join(",")
            );
        }

        // Vision by storage index: bit 1 for player 1, bit 2 for player 2.
        // The player whose turn it is sees what the engine tracked this turn.
        let vision = |p: u8| {
            if game.player == p {
                game.visible_hexes().to_vec()
            } else {
                game.vision(p)
            }
        };
        let (v1, v2) = (vision(1), vision(2));
        let vis: String = (0..game.map.len())
            .map(|i| char::from(b'0' + v1[i] as u8 + 2 * v2[i] as u8))
            .collect();
        let _ = write!(out, "],\"vis\":{}", esc(&vis));

        let reply: Vec<String> = reply.iter().map(|r| esc(r)).collect();
        let _ = write!(out, ",\"reply\":[{}]", reply.join(","));
        match &game.outcome {
            Some(o) => {
                let _ = write!(out, ",\"outcome\":[{},{}]}}", o.winner, esc(&o.reason));
            }
            None => out.push_str(",\"outcome\":null}"),
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seeks_match_a_straight_run() {
        let rules = bitbath_engine::Rules::default();
        let map = bitbath_engine::mapgen::generate(&rules, 3).unwrap();
        let game = Game::new(rules, map);
        let setup = game.setup_lines(true).join("\n");
        let script = "T 1\nO M 3 000\nO B 0 I\nT 2\nO M 6 333\nT 1\nO M 99 0\n";
        let (viewer, info) = load(&setup, script, false).unwrap();
        assert!(info.starts_with("{\"width\":40,"));
        // One army entry at the start and one after each of the 3 turns;
        // each side starts with two infantry and a scout.
        let army = &info[info.find("\"army\":").unwrap()..];
        assert_eq!(army.matches('[').count(), 1 + 4, "{army}");
        assert!(army.starts_with("\"army\":[[280,280],"), "{army}");
        assert_eq!(viewer.steps.len(), 7);
        assert_eq!(viewer.snapshots.len(), 3);
        for k in 0..=7 {
            let s = viewer.state_json(k);
            assert!(s.starts_with(&format!("{{\"step\":{k},")), "{s}");
        }
        assert!(viewer.state_json(7).contains("\"reply\":[\"ERR unknown"));
        let s3 = viewer.state_json(3);
        assert!(
            s3.contains("\"reply\":[\"OK "),
            "{}",
            &s3[s3.find("\"reply\"").unwrap()..]
        );
        assert!(load(&setup, "X 1", false).is_err());
    }

    #[test]
    fn the_last_step_reaches_the_round_limit() {
        let mut rules = bitbath_engine::Rules::default();
        rules.c.round_limit = 1;
        let map = bitbath_engine::mapgen::generate(&rules, 3).unwrap();
        let game = Game::new(rules, map);
        let setup = game.setup_lines(true).join("\n");
        let (viewer, _) = load(&setup, "T 1\nO B 0 I\nT 2\n", false).unwrap();
        assert!(viewer.state_json(2).ends_with("\"outcome\":null}"));
        let (unfinished, _) = load(&setup, "T 1\nO B 0 I\nT 2\n", true).unwrap();
        assert!(unfinished.state_json(3).ends_with("\"outcome\":null}"));
        // Player 1 built a unit, so the tiebreak gives them the bigger army.
        assert!(viewer.state_json(3).ends_with("\"outcome\":[1,\"army\"]}"));
    }

    #[test]
    fn a_live_game_matches_its_replay() {
        let config = "map_width = 20\nmap_height = 14 # small\nneutral_cities = 4\nround_limit = 1\nseed = 5";
        let setup = new_setup(config).unwrap();
        assert!(setup.starts_with("MAP 20 14\n"), "{setup}");
        assert!(new_setup("nope = 1").is_err());
        assert!(new_setup("map_height = 7").is_err());

        let (mut live, _) = load(&setup, "", false).unwrap();
        let mut script = String::new();
        let mut replies = Vec::new();
        for line in ["T 1", "O B 0 I", "O M 99 0", "T 2", "O B 1 S"] {
            let reply = live.push(parse_step(line).unwrap().unwrap());
            replies.push(reply.last().cloned().unwrap_or_default());
            script.push_str(line);
            script.push('\n');
            // The turn is open, so the round limit hasn't ended the game.
            assert!(live.state_json(live.steps.len()).ends_with("\"outcome\":null}"));
        }
        assert!(replies[1].starts_with("OK "), "{replies:?}");
        assert!(replies[2].starts_with("ERR unknown"), "{replies:?}");
        assert_eq!(live.tip.turn_message(0, 0)[0], "TURN 1");

        assert!(live.end());
        let (replay, info) = load(&setup, &script, false).unwrap();
        for k in 0..=5 {
            assert_eq!(live.state_json(k), replay.state_json(k));
        }
        // Player 1's infantry is worth more than player 2's scout.
        assert!(live.state_json(5).ends_with("\"outcome\":[1,\"army\"]}"));
        for reply in &replies {
            assert!(info.contains(&esc(reply)), "{reply}");
        }
    }
}
