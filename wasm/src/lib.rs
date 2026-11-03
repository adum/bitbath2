//! The engine compiled to wasm for the replay viewer (SPEC §8).
//!
//! This exposes a small C ABI instead of using wasm-bindgen, so building it
//! needs only cargo and the `wasm32-unknown-unknown` target. Strings cross
//! the boundary as UTF-8 in wasm memory: JavaScript copies inputs into
//! buffers from `bb_alloc` and reads outputs at `bb_out_ptr`/`bb_out_len`.

use bitbath_engine::Game;
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
) -> i32 {
    let setup = input(setup_ptr, setup_len);
    let script = input(script_ptr, script_len);
    match load(&setup, &script) {
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

fn load(setup: &str, script: &str) -> Result<(Viewer, String), String> {
    let game = Game::from_setup(setup.lines())?;
    let mut steps = Vec::new();
    for line in script.lines() {
        if let Some(p) = line.strip_prefix("T ") {
            let player = p.trim().parse().map_err(|_| format!("bad step '{line}'"))?;
            if player != 1 && player != 2 {
                return Err(format!("bad player in '{line}'"));
            }
            steps.push(Step::Turn(player));
        } else if let Some(order) = line.strip_prefix("O ") {
            steps.push(Step::Order(order.to_string()));
        } else if !line.trim().is_empty() {
            return Err(format!("bad step '{line}'"));
        }
    }
    let mut info = info_json(&game);
    let mut snapshots = vec![(0, game.clone())];
    // Each step's result line (empty for turn starts), so the viewer can
    // list a turn's orders with their outcomes without seeking to each.
    let mut results = Vec::with_capacity(steps.len());
    let mut g = game;
    for (i, step) in steps.iter().enumerate() {
        if i > 0 && matches!(step, Step::Turn(_)) {
            snapshots.push((i, g.clone()));
        }
        let reply = apply(&mut g, step);
        results.push(esc(reply.last().map_or("", String::as_str)));
    }
    info.pop();
    let _ = write!(info, ",\"results\":[{}]}}", results.join(","));
    Ok((Viewer { steps, snapshots }, info))
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
                "{}:{{\"name\":{},\"move_cost\":{},\"defense\":{},\"vision_bonus\":{}}}",
                esc(&t.ch.to_string()),
                esc(&t.name),
                t.move_cost,
                t.defense,
                t.vision_bonus
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
    let _ = write!(s, "],\"capture_points\":{}}}", game.rules.c.capture_points);
    s
}

impl Viewer {
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

        let mut out = format!(
            "{{\"step\":{step},\"round\":{},\"player\":{},\"gold\":[{},{}],\"units\":[",
            game.round, game.player, game.gold[1], game.gold[2]
        );
        let units: Vec<String> = game
            .units
            .iter()
            .map(|u| {
                let flags = u.acted as u8 | (u.moved as u8) << 1 | (u.fresh as u8) << 2;
                format!(
                    "[{},{},{},{},{},{},{},{}]",
                    u.id,
                    u.owner,
                    esc(&game.rules.units[u.ty].code.to_string()),
                    u.hex.q,
                    u.hex.r,
                    u.hp,
                    u.mp,
                    flags
                )
            })
            .collect();
        out.push_str(&units.join(","));
        out.push_str("],\"cities\":[");
        let cities: Vec<String> = game
            .cities
            .iter()
            .map(|c| format!("[{},{},{}]", c.owner, c.capturer, c.progress))
            .collect();
        out.push_str(&cities.join(","));

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
        let (viewer, info) = load(&setup, script).unwrap();
        assert!(info.starts_with("{\"width\":40,"));
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
        assert!(load(&setup, "X 1").is_err());
    }
}
