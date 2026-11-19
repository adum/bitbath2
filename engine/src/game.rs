//! Game state and order processing (SPEC §3.4–§3.8, §4, §6.3–§6.4).

use crate::hex::Hex;
use crate::map::{parse_setup, Map};
use crate::rules::Rules;
use crate::PROTOCOL_VERSION;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Unit {
    pub id: u32,
    pub owner: u8,
    /// Index into `Rules::units`.
    pub ty: usize,
    pub hex: Hex,
    pub hp: u32,
    pub mp: u32,
    /// Attacked or captured this turn.
    pub acted: bool,
    /// Changed hexes this turn.
    pub moved: bool,
    /// Built this turn, so it can't move or act until its owner's next turn.
    pub fresh: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct City {
    pub id: u32,
    pub hex: Hex,
    pub owner: u8,
    /// The player with capture progress here, or 0.
    pub capturer: u8,
    pub progress: u32,
    /// Built a unit or was captured this turn, so it can't build until its owner's next turn.
    pub built: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    /// 1 or 2, or 0 for a draw.
    pub winner: u8,
    /// `elimination`, `cities`, `army`, or `draw`, or why a bot forfeited,
    /// such as `crash` or `hang`.
    pub reason: String,
}

/// A rejected order: an `ERR` code and a plain-English explanation.
struct Reject {
    code: &'static str,
    msg: String,
}

fn reject<T>(code: &'static str, msg: impl Into<String>) -> Result<T, Reject> {
    Err(Reject { code, msg: msg.into() })
}

fn number(tok: &str, what: &str) -> Result<u32, Reject> {
    tok.parse()
        .or_else(|_| reject("syntax", format!("'{tok}' is not a valid {what}")))
}

/// Damage dealt by a unit of type `att` with `att_hp` HP to a unit of type
/// `def` standing on terrain with `defense` percent (SPEC §3.7). Every
/// operand is non-negative, so kits get the same result with floor division.
pub fn damage(rules: &Rules, att: usize, att_hp: u32, def: usize, defense: u32) -> u32 {
    let base = rules.damage[att][def];
    if base == 0 {
        return 0;
    }
    let dealt = base as u64 * att_hp as u64 * (100 - defense.min(100)) as u64 / (rules.units[att].max_hp as u64 * 100);
    dealt.max(1) as u32
}

#[derive(Clone, Debug)]
pub struct Game {
    pub rules: Rules,
    pub map: Map,
    /// Live units, sorted by id.
    pub units: Vec<Unit>,
    pub cities: Vec<City>,
    /// Gold by player. Index 0 is unused.
    pub gold: [u32; 3],
    /// The current round, from 1. It is 0 before the first turn.
    pub round: u32,
    /// The player whose turn it is, or 0 before the first turn.
    pub player: u8,
    pub next_unit_id: u32,
    pub outcome: Option<Outcome>,
    /// Hexes the current player can see. Vision only grows during a turn.
    visible: Vec<bool>,
    /// Unit ids on each hex. A hex never holds units of both players.
    occupants: Vec<Vec<u32>>,
    /// The city on each hex, if any.
    city_at: Vec<Option<u32>>,
    /// `LOG` lines waiting for each player's next turn message.
    logs: [Vec<String>; 3],
}

impl Game {
    /// A new game, with each player's starting units on their home city
    /// (their lowest-numbered city).
    pub fn new(rules: Rules, map: Map) -> Game {
        let mut g = Game::empty(rules, map);
        for player in 1..=2u8 {
            if let Some(home) = g.cities.iter().find(|c| c.owner == player).map(|c| c.hex) {
                for code in g.rules.start_units.clone() {
                    if let Some(ty) = g.rules.unit_index(code) {
                        g.spawn(player, ty, home, false);
                    }
                }
            }
        }
        g
    }

    /// A new game from setup lines (SPEC §6.2), on top of the default rules.
    pub fn from_setup<'a>(lines: impl IntoIterator<Item = &'a str>) -> Result<Game, String> {
        let (rules, map) = parse_setup(lines, Rules::default())?;
        Ok(Game::new(rules, map))
    }

    /// A game with the map's cities but no units, for tests and scenarios.
    pub fn empty(rules: Rules, map: Map) -> Game {
        let n = map.len();
        let cities: Vec<City> = map
            .cities
            .iter()
            .enumerate()
            .map(|(i, c)| City {
                id: i as u32,
                hex: c.hex,
                owner: c.owner,
                capturer: 0,
                progress: 0,
                built: false,
            })
            .collect();
        let mut city_at = vec![None; n];
        for c in &cities {
            city_at[map.index(c.hex)] = Some(c.id);
        }
        let start_gold = rules.c.start_gold;
        Game {
            rules,
            map,
            units: Vec::new(),
            cities,
            gold: [0, start_gold, start_gold],
            round: 0,
            player: 0,
            next_unit_id: 1,
            outcome: None,
            visible: vec![false; n],
            occupants: vec![Vec::new(); n],
            city_at,
            logs: Default::default(),
        }
    }

    /// Adds a full-HP unit and returns its id.
    pub fn spawn(&mut self, owner: u8, ty: usize, hex: Hex, fresh: bool) -> u32 {
        let id = self.next_unit_id;
        self.next_unit_id += 1;
        let hp = self.rules.units[ty].max_hp;
        self.units.push(Unit {
            id,
            owner,
            ty,
            hex,
            hp,
            mp: 0,
            acted: false,
            moved: false,
            fresh,
        });
        let i = self.map.index(hex);
        self.occupants[i].push(id);
        id
    }

    /// Adds a unit with a chosen id and HP, for scenarios. Ids must increase.
    pub fn place(&mut self, id: u32, owner: u8, ty: usize, hex: Hex, hp: u32) -> Result<(), String> {
        if id < self.next_unit_id {
            return Err(format!("unit ids must increase; got {id}"));
        }
        if owner != 1 && owner != 2 {
            return Err(format!("unit {id}: owner must be 1 or 2"));
        }
        if ty >= self.rules.units.len() || hp == 0 || hp > self.rules.units[ty].max_hp {
            return Err(format!("unit {id}: bad type or hp"));
        }
        if !self.map.contains(hex) || !self.rules.terrains[self.map.terrain_at(hex)].passable() {
            return Err(format!("unit {id}: {hex} isn't a passable hex on the map"));
        }
        let i = self.map.index(hex);
        if self.occupants[i].iter().any(|&o| self.owner_of(o) != owner) {
            return Err(format!("unit {id}: {hex} holds the other player's units"));
        }
        self.next_unit_id = id;
        self.spawn(owner, ty, hex, false);
        self.units.last_mut().expect("just spawned").hp = hp;
        Ok(())
    }

    pub fn unit(&self, id: u32) -> Option<&Unit> {
        self.unit_idx(id).map(|i| &self.units[i])
    }

    /// Ids of the units on a hex on the map.
    pub fn units_at(&self, hex: Hex) -> &[u32] {
        &self.occupants[self.map.index(hex)]
    }

    /// Hexes the current player can see this turn, by storage index.
    pub fn visible_hexes(&self) -> &[bool] {
        &self.visible
    }

    pub fn cities_of(&self, player: u8) -> usize {
        self.cities.iter().filter(|c| c.owner == player).count()
    }

    pub fn units_of(&self, player: u8) -> usize {
        self.units.iter().filter(|u| u.owner == player).count()
    }

    /// Σ cost × hp / max_hp over a player's units, used by the tiebreak.
    pub fn army_value(&self, player: u8) -> u64 {
        self.units
            .iter()
            .filter(|u| u.owner == player)
            .map(|u| {
                let t = &self.rules.units[u.ty];
                t.cost as u64 * u.hp as u64 / t.max_hp as u64
            })
            .sum()
    }

    /// Hexes `player` can see from their current units and cities (SPEC §4).
    pub fn vision(&self, player: u8) -> Vec<bool> {
        let mut vis = vec![false; self.map.len()];
        let mut mark = |center: Hex, radius: u32| {
            for h in center.disk(radius) {
                if self.map.contains(h) && self.sees(center, radius, h) {
                    vis[self.map.index(h)] = true;
                }
            }
        };
        for i in 0..self.units.len() {
            if self.units[i].owner == player {
                mark(self.units[i].hex, self.unit_vision(i));
            }
        }
        for c in &self.cities {
            if c.owner == player {
                mark(c.hex, self.rules.c.city_vision);
            }
        }
        vis
    }

    /// Starts `player`'s turn: income, healing, capture reset, refresh, and
    /// vision, in that order (SPEC §3.4). Player 1's turn starts a new round.
    pub fn begin_turn(&mut self, player: u8) {
        assert!(player == 1 || player == 2, "player must be 1 or 2");
        if player == 1 {
            self.round += 1;
        }
        self.player = player;
        let p = player as usize;

        let cities = self.cities_of(player) as i64;
        let units = self.units_of(player) as i64;
        let c = &self.rules.c;
        let gold = self.gold[p] as i64 + cities * c.city_income as i64 - units * c.unit_upkeep as i64;
        self.gold[p] = gold.clamp(0, u32::MAX as i64) as u32;

        for i in 0..self.units.len() {
            let u = &self.units[i];
            let on_own_city =
                self.city_at[self.map.index(u.hex)].is_some_and(|cid| self.cities[cid as usize].owner == player);
            if u.owner == player && on_own_city {
                let max_hp = self.rules.units[u.ty].max_hp;
                let u = &mut self.units[i];
                u.hp = (u.hp + self.rules.c.city_heal).min(max_hp);
            }
        }

        for ci in 0..self.cities.len() {
            let capturer = self.cities[ci].capturer;
            if capturer != 0 && !self.has_unit_of(self.cities[ci].hex, capturer) {
                self.cities[ci].capturer = 0;
                self.cities[ci].progress = 0;
            }
        }

        for u in self.units.iter_mut() {
            if u.owner == player {
                u.mp = self.rules.units[u.ty].mv;
                u.acted = false;
                u.moved = false;
                u.fresh = false;
            }
        }
        for c in self.cities.iter_mut() {
            c.built = false;
        }

        self.visible = self.vision(player);
    }

    /// Ends the current turn. After the last round, the tiebreak decides.
    pub fn end_turn(&mut self) {
        self.logs[self.player as usize].clear();
        if self.outcome.is_none() && self.player == 2 && self.round >= self.rules.c.round_limit {
            self.outcome = Some(self.tiebreak());
        }
    }

    fn tiebreak(&self) -> Outcome {
        let (c1, c2) = (self.cities_of(1), self.cities_of(2));
        if c1 != c2 {
            return Outcome {
                winner: if c1 > c2 { 1 } else { 2 },
                reason: "cities".into(),
            };
        }
        let (a1, a2) = (self.army_value(1), self.army_value(2));
        if a1 != a2 {
            return Outcome {
                winner: if a1 > a2 { 1 } else { 2 },
                reason: "army".into(),
            };
        }
        Outcome {
            winner: 0,
            reason: "draw".into(),
        }
    }

    /// `player` forfeits, unless the game is already over.
    pub fn forfeit(&mut self, player: u8, reason: &str) {
        self.finish(3 - player, reason);
    }

    /// Ends the game, unless it is already over. `winner` 0 is a draw.
    pub fn finish(&mut self, winner: u8, reason: &str) {
        if self.outcome.is_none() {
            self.outcome = Some(Outcome {
                winner,
                reason: reason.into(),
            });
        }
    }

    /// The map and rules as setup lines. `with_start` adds the `START` line.
    pub fn setup_lines(&self, with_start: bool) -> Vec<String> {
        let mut out = self.map.lines(&self.rules);
        out.extend(self.rules.lines(with_start));
        out
    }

    /// The init message for `player` (SPEC §6.2), without line endings.
    pub fn init_message(&self, player: u8) -> Vec<String> {
        let mut out = vec![format!("BITBATH {PROTOCOL_VERSION}"), format!("PLAYER {player}")];
        out.extend(self.setup_lines(false));
        out.push("INIT".into());
        out
    }

    /// The current player's turn message (SPEC §6.3). Call after `begin_turn`.
    pub fn turn_message(&self, time_ms: u64, bank_ms: u64) -> Vec<String> {
        let p = self.player;
        let mut out = vec![
            format!("TURN {}", self.round),
            format!("TIME {time_ms} {bank_ms}"),
            format!("GOLD {}", self.gold[p as usize]),
        ];
        for c in &self.cities {
            let (capturer, progress) = if self.visible[self.map.index(c.hex)] {
                (c.capturer, c.progress)
            } else {
                (0, 0)
            };
            out.push(format!("CITY {} {} {} {}", c.id, c.owner, capturer, progress));
        }
        let mine = self.units.iter().filter(|u| u.owner == p);
        let seen = self
            .units
            .iter()
            .filter(|u| u.owner != p && self.visible[self.map.index(u.hex)]);
        for u in mine.chain(seen) {
            let code = self.rules.units[u.ty].code;
            out.push(format!(
                "UNIT {} {} {} {} {} {}",
                u.id, u.owner, code, u.hex.q, u.hex.r, u.hp
            ));
        }
        out.extend(self.logs[p as usize].iter().cloned());
        out.push("GO".into());
        out
    }

    /// Runs one order line for the current player and returns the reply:
    /// zero or more `SEE` events, then one `OK`, `PART`, or `ERR` line.
    pub fn apply(&mut self, line: &str) -> Vec<String> {
        let mut out = Vec::new();
        let t: Vec<&str> = line.split_whitespace().collect();
        let result = if self.outcome.is_some() {
            reject("late", "the game is over")
        } else if self.player == 0 {
            reject("late", "no turn is in progress")
        } else {
            match t.first().copied() {
                Some("M") => self.order_move(&t, &mut out),
                Some("A") => self.order_attack(&t, &mut out),
                Some("C") => self.order_capture(&t, &mut out),
                Some("B") => self.order_build(&t, &mut out),
                Some(other) => reject(
                    "syntax",
                    format!("unknown order '{other}'; orders are M, A, C, B, D, and END"),
                ),
                None => reject("syntax", "empty order"),
            }
        };
        if let Err(r) = result {
            out.push(format!("ERR {} {}", r.code, r.msg));
        }
        out
    }

    fn order_move(&mut self, t: &[&str], out: &mut Vec<String>) -> Result<(), Reject> {
        if t.len() != 3 {
            return reject("syntax", "usage: M <unit> <dirs>");
        }
        let id = number(t[1], "unit id")?;
        let mut dirs = Vec::with_capacity(t[2].len());
        for b in t[2].bytes() {
            if !(b'0'..=b'5').contains(&b) {
                return reject("syntax", format!("'{}' isn't a path; use direction digits 0-5", t[2]));
            }
            dirs.push((b - b'0') as usize);
        }
        let ui = self.own_unit(id)?;
        self.check_ready(ui)?;
        let (start, mp, ty) = (self.units[ui].hex, self.units[ui].mp, self.units[ui].ty);
        if mp == 0 {
            return reject("no_mp", format!("unit {id} has no movement points left"));
        }
        let skirmish = self.rules.units[ty].skirmish;

        // Check the whole path against what the player can see.
        let mut pos = start;
        let mut cost = 0;
        for (k, &dir) in dirs.iter().enumerate() {
            let next = pos.step(dir);
            if !self.map.contains(next) {
                return reject("path", format!("step {} leaves the map at {next}", k + 1));
            }
            let terrain = &self.rules.terrains[self.map.terrain_at(next)];
            if !terrain.passable() {
                return reject("path", format!("{next} is {}", terrain.name));
            }
            let i = self.map.index(next);
            if self.visible[i] && self.enemy_at(i) {
                return reject("path", format!("{next} holds an enemy unit"));
            }
            cost += terrain.move_cost;
            if k + 1 < dirs.len() && !skirmish && self.in_zoc(next, true) {
                return reject(
                    "zoc",
                    format!("{next} is in an enemy zone of control; a path may only enter one on its last step"),
                );
            }
            pos = next;
        }
        if cost > mp {
            return reject("no_mp", format!("the path costs {cost} MP but unit {id} has {mp}"));
        }
        let dest = self.map.index(pos);
        let friends = self.occupants[dest]
            .iter()
            .filter(|&&o| o != id && self.owner_of(o) == self.player)
            .count();
        if friends as u32 >= self.rules.c.stack_limit {
            return reject("stack_full", format!("{pos} already holds {friends} units"));
        }

        // Move step by step. Only hidden enemies can cut the move short.
        let mut pos = start;
        for (k, &dir) in dirs.iter().enumerate() {
            let next = pos.step(dir);
            let i = self.map.index(next);
            if self.enemy_at(i) {
                self.units[ui].mp = 0;
                self.reveal(next, 0, out);
                out.push(format!("PART {} {} ambush", pos.q, pos.r));
                return Ok(());
            }
            self.units[ui].mp -= self.rules.terrains[self.map.tiles[i] as usize].move_cost;
            self.relocate(ui, next);
            self.units[ui].moved = true;
            let radius = self.unit_vision(ui);
            self.reveal(next, radius, out);
            if !skirmish && self.in_zoc(next, false) {
                self.units[ui].mp = 0;
                if k + 1 < dirs.len() {
                    out.push(format!("PART {} {} zoc", next.q, next.r));
                    return Ok(());
                }
            }
            pos = next;
        }
        out.push(format!("OK {} {} {}", pos.q, pos.r, self.units[ui].mp));
        Ok(())
    }

    fn order_attack(&mut self, t: &[&str], out: &mut Vec<String>) -> Result<(), Reject> {
        if t.len() != 3 {
            return reject("syntax", "usage: A <unit> <target>");
        }
        let id = number(t[1], "unit id")?;
        let target = number(t[2], "target id")?;
        let ai = self.own_unit(id)?;
        self.check_ready(ai)?;
        let (aty, a_hex, a_hp) = (self.units[ai].ty, self.units[ai].hex, self.units[ai].hp);
        let at = &self.rules.units[aty];
        if at.no_move_attack && self.units[ai].moved {
            return reject(
                "acted",
                format!("unit {id} moved this turn, and {}s can't attack after moving", at.name),
            );
        }
        let di = match self.unit_idx(target) {
            Some(i) if self.units[i].owner != self.player && self.visible[self.map.index(self.units[i].hex)] => i,
            _ => return reject("no_target", format!("there is no visible enemy unit {target}")),
        };
        let (dty, d_hex, d_hp) = (self.units[di].ty, self.units[di].hex, self.units[di].hp);
        let dist = a_hex.distance(d_hex);
        if !at.in_range(dist) {
            return reject(
                "range",
                format!(
                    "unit {target} is {dist} hexes away; unit {id} attacks at range {}..{}",
                    at.range_min, at.range_max
                ),
            );
        }
        let code = at.code;

        let defense = |h: Hex| self.rules.terrains[self.map.terrain_at(h)].defense;
        let d_hp = d_hp.saturating_sub(damage(&self.rules, aty, a_hp, dty, defense(d_hex)));
        let mut a_hp = a_hp;
        if d_hp > 0 && self.rules.units[dty].in_range(dist) && self.rules.damage[dty][aty] > 0 {
            a_hp = a_hp.saturating_sub(damage(&self.rules, dty, d_hp, aty, defense(a_hex)));
        }

        let attacker = &mut self.units[ai];
        attacker.hp = a_hp;
        attacker.acted = true;
        attacker.mp = 0;
        self.units[di].hp = d_hp;
        let victim = self.units[di].owner as usize;
        self.logs[victim].push(format!(
            "LOG HIT {id} {code} {} {} {target} {d_hp} {a_hp}",
            a_hex.q, a_hex.r
        ));
        if d_hp == 0 {
            self.remove_unit(target);
        } else if a_hp == 0 {
            self.remove_unit(id);
        }
        out.push(format!("OK {d_hp} {a_hp}"));
        Ok(())
    }

    fn order_capture(&mut self, t: &[&str], out: &mut Vec<String>) -> Result<(), Reject> {
        if t.len() != 2 {
            return reject("syntax", "usage: C <unit>");
        }
        let id = number(t[1], "unit id")?;
        let ui = self.own_unit(id)?;
        self.check_ready(ui)?;
        let (hex, hp, ty) = (self.units[ui].hex, self.units[ui].hp, self.units[ui].ty);
        if !self.rules.units[ty].capture {
            return reject(
                "cannot_capture",
                format!("{}s can't capture cities", self.rules.units[ty].name),
            );
        }
        let Some(ci) = self.city_at[self.map.index(hex)].map(|c| c as usize) else {
            return reject("cannot_capture", format!("unit {id} isn't on a city"));
        };
        let player = self.player;
        if self.cities[ci].owner == player {
            return reject("cannot_capture", format!("you already own city {ci}"));
        }

        let capture_points = self.rules.c.capture_points;
        let city = &mut self.cities[ci];
        if city.capturer != player {
            city.capturer = player;
            city.progress = 0;
        }
        city.progress += hp;
        self.units[ui].acted = true;
        if self.cities[ci].progress < capture_points {
            out.push(format!("OK {} {capture_points}", self.cities[ci].progress));
            return Ok(());
        }

        let city = &mut self.cities[ci];
        let old = city.owner;
        city.owner = player;
        city.capturer = 0;
        city.progress = 0;
        city.built = true;
        self.logs[(3 - player) as usize].push(format!("LOG CITY {ci} {old} {player}"));
        self.reveal(hex, self.rules.c.city_vision, out);
        out.push("OK captured".into());
        if old != 0 && self.cities_of(old) == 0 {
            self.finish(player, "elimination");
        }
        Ok(())
    }

    fn order_build(&mut self, t: &[&str], out: &mut Vec<String>) -> Result<(), Reject> {
        if t.len() != 3 {
            return reject("syntax", "usage: B <city> <type>");
        }
        let ci = number(t[1], "city id")? as usize;
        if ci >= self.cities.len() {
            return reject("unknown", format!("there is no city {ci}"));
        }
        let owner = self.cities[ci].owner;
        if owner != self.player {
            let whose = if owner == 0 {
                "nobody".to_string()
            } else {
                format!("player {owner}")
            };
            return reject("not_yours", format!("city {ci} belongs to {whose}"));
        }
        let mut chars = t[2].chars();
        let ty = match (chars.next(), chars.next()) {
            (Some(code), None) => self.rules.unit_index(code),
            _ => None,
        };
        let Some(ty) = ty else {
            return reject("unknown", format!("there is no unit type '{}'", t[2]));
        };
        if self.cities[ci].built {
            return reject(
                "city_used",
                format!("city {ci} already built a unit or was captured this turn"),
            );
        }
        let hex = self.cities[ci].hex;
        let i = self.map.index(hex);
        if self.enemy_at(i) {
            return reject("city_used", format!("an enemy unit stands on city {ci}"));
        }
        let here = self.occupants[i].len();
        if here as u32 >= self.rules.c.stack_limit {
            return reject("stack_full", format!("city {ci} already holds {here} units"));
        }
        let (cost, name) = (self.rules.units[ty].cost, &self.rules.units[ty].name);
        let p = self.player as usize;
        if self.gold[p] < cost {
            return reject(
                "no_gold",
                format!("a {name} costs {cost} gold and you have {}", self.gold[p]),
            );
        }

        self.gold[p] -= cost;
        self.cities[ci].built = true;
        let id = self.spawn(self.player, ty, hex, true);
        let radius = self.unit_vision(self.units.len() - 1);
        self.reveal(hex, radius, out);
        out.push(format!("OK {id}"));
        Ok(())
    }

    fn unit_idx(&self, id: u32) -> Option<usize> {
        self.units.binary_search_by_key(&id, |u| u.id).ok()
    }

    fn owner_of(&self, id: u32) -> u8 {
        self.units[self.unit_idx(id).expect("occupants are live units")].owner
    }

    /// The current player's unit `id`. Hidden enemy units look the same as
    /// units that don't exist, so probing ids reveals nothing.
    fn own_unit(&self, id: u32) -> Result<usize, Reject> {
        match self.unit_idx(id) {
            Some(i) if self.units[i].owner == self.player => Ok(i),
            Some(i) if self.visible[self.map.index(self.units[i].hex)] => reject(
                "not_yours",
                format!("unit {id} belongs to player {}", self.units[i].owner),
            ),
            _ => reject("unknown", format!("you have no unit {id}")),
        }
    }

    fn check_ready(&self, i: usize) -> Result<(), Reject> {
        let u = &self.units[i];
        if u.fresh {
            return reject(
                "new_unit",
                format!("unit {} was built this turn and can't act until your next turn", u.id),
            );
        }
        if u.acted {
            return reject("acted", format!("unit {} already attacked or captured this turn", u.id));
        }
        Ok(())
    }

    fn has_unit_of(&self, hex: Hex, player: u8) -> bool {
        self.occupants[self.map.index(hex)]
            .iter()
            .any(|&id| self.owner_of(id) == player)
    }

    /// Whether the hex at storage index `i` holds the current player's enemies.
    fn enemy_at(&self, i: usize) -> bool {
        self.occupants[i]
            .first()
            .is_some_and(|&id| self.owner_of(id) != self.player)
    }

    /// Whether `hex` is next to an enemy unit with `zoc`. With `visible_only`,
    /// only enemies the current player can see count.
    fn in_zoc(&self, hex: Hex, visible_only: bool) -> bool {
        hex.neighbors().into_iter().any(|nb| {
            if !self.map.contains(nb) {
                return false;
            }
            let i = self.map.index(nb);
            (!visible_only || self.visible[i])
                && self.occupants[i].iter().any(|&id| {
                    let u = &self.units[self.unit_idx(id).expect("occupants are live units")];
                    u.owner != self.player && self.rules.units[u.ty].zoc
                })
        })
    }

    fn unit_vision(&self, i: usize) -> u32 {
        let u = &self.units[i];
        self.rules.units[u.ty].vision + self.rules.terrains[self.map.terrain_at(u.hex)].vision_bonus
    }

    /// Whether an observer at `from` with vision `radius` sees the map hex
    /// `to` (SPEC §4). Concealing terrain shortens the range to that hex,
    /// but never hides the hexes next to the observer.
    fn sees(&self, from: Hex, radius: u32, to: Hex) -> bool {
        let d = from.distance(to);
        d <= radius && (d <= 1 || d + self.rules.terrains[self.map.terrain_at(to)].conceal <= radius)
    }

    /// Makes the hexes an observer at `center` with vision `radius` sees
    /// visible to the current player, adding a `SEE` line for each enemy
    /// unit that comes into view.
    fn reveal(&mut self, center: Hex, radius: u32, out: &mut Vec<String>) {
        let mut seen = Vec::new();
        for h in center.disk(radius) {
            if !self.map.contains(h) || !self.sees(center, radius, h) {
                continue;
            }
            let i = self.map.index(h);
            if !self.visible[i] {
                self.visible[i] = true;
                seen.extend_from_slice(&self.occupants[i]);
            }
        }
        seen.sort_unstable();
        for id in seen {
            let u = &self.units[self.unit_idx(id).expect("occupants are live units")];
            if u.owner != self.player {
                let code = self.rules.units[u.ty].code;
                out.push(format!(
                    "SEE {} {} {} {} {} {}",
                    u.id, u.owner, code, u.hex.q, u.hex.r, u.hp
                ));
            }
        }
    }

    fn relocate(&mut self, i: usize, to: Hex) {
        let id = self.units[i].id;
        let from = self.map.index(self.units[i].hex);
        self.occupants[from].retain(|&o| o != id);
        let dest = self.map.index(to);
        self.occupants[dest].push(id);
        self.units[i].hex = to;
    }

    fn remove_unit(&mut self, id: u32) {
        if let Some(i) = self.unit_idx(id) {
            let u = self.units.remove(i);
            let at = self.map.index(u.hex);
            self.occupants[at].retain(|&o| o != id);
        }
    }

    /// Checks internal bookkeeping. Used by tests.
    pub fn check_invariants(&self) -> Result<(), String> {
        if !self.units.windows(2).all(|w| w[0].id < w[1].id) {
            return Err("units aren't sorted by id".into());
        }
        let mut count = 0;
        for (i, ids) in self.occupants.iter().enumerate() {
            count += ids.len();
            let hex = self.map.hex(i);
            let mut owners = ids.iter().map(|&id| self.unit(id).map(|u| (u.owner, u.hex)));
            if owners.any(|o| o.is_none_or(|(_, h)| h != hex)) {
                return Err(format!("occupants of {hex} are out of date"));
            }
            let first = ids.first().map(|&id| self.owner_of(id));
            if ids.iter().any(|&id| Some(self.owner_of(id)) != first) {
                return Err(format!("{hex} holds units of both players"));
            }
        }
        if count != self.units.len() {
            return Err("occupant count doesn't match the unit count".into());
        }
        for u in &self.units {
            let t = &self.rules.units[u.ty];
            if u.hp == 0 || u.hp > t.max_hp || u.mp > t.mv {
                return Err(format!("unit {} has hp {} and mp {}", u.id, u.hp, u.mp));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worked_example() {
        // SPEC §3.7: full-HP infantry on plains attacks infantry on hills.
        let r = Rules::default();
        assert_eq!(damage(&r, 0, 100, 0, 40), 33);
        assert_eq!(damage(&r, 0, 67, 0, 0), 36);
        assert_eq!(damage(&r, 4, 1, 2, 30), 1);
    }

    #[test]
    fn first_turn_matches_spec_example() {
        let g = Game::new(Rules::default(), crate::mapgen::generate(&Rules::default(), 7).unwrap());
        let mut g = g;
        g.begin_turn(1);
        assert_eq!(g.gold[1], 385);
        let ids: Vec<u32> = g.units.iter().map(|u| u.id).collect();
        assert_eq!(ids, vec![1, 2, 3, 4, 5, 6]);
        let msg = g.turn_message(6000, 5000);
        assert_eq!(msg[0], "TURN 1");
        assert_eq!(msg.last().unwrap(), "GO");
        g.check_invariants().unwrap();
    }

    #[test]
    fn a_captured_city_builds_from_its_owners_next_turn() {
        let rules = Rules::default();
        let map = crate::mapgen::generate(&rules, 7).unwrap();
        let mut g = Game::empty(rules, map);
        let ci = g.cities.iter().position(|c| c.owner == 0).unwrap();
        let infantry = g.rules.unit_index('I').unwrap();
        let id = g.spawn(1, infantry, g.cities[ci].hex, false);
        g.cities[ci].capturer = 1;
        g.cities[ci].progress = 150;
        g.begin_turn(1);
        assert_eq!(g.apply(&format!("C {id}")).last().unwrap(), "OK captured");
        let reply = g.apply(&format!("B {ci} I"));
        assert!(reply.last().unwrap().starts_with("ERR city_used "), "{reply:?}");
        g.end_turn();
        g.begin_turn(2);
        g.end_turn();
        g.begin_turn(1);
        let reply = g.apply(&format!("B {ci} I"));
        assert!(reply.last().unwrap().starts_with("OK "), "{reply:?}");
    }
}
