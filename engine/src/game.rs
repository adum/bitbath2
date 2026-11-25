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
    /// Fortification level: a `fortify` unit gains one for each of its
    /// owner's turns it spends without moving or acting, up to
    /// `fortify_max`, and loses them all when it moves (SPEC §3.7).
    pub fort: u32,
    /// Attacked this turn. With supply on, that makes its turn cost more food.
    pub attacked: bool,
    /// Rations left, with supply on (STRATEGIC.md §5).
    pub rations: u32,
    /// Wheat and bread carried by a `carry` unit (STRATEGIC.md §4).
    pub wheat: u32,
    pub bread: u32,
    /// The transport this unit is aboard, or 0 (STRATEGIC.md §6). A unit
    /// aboard has its transport's hex.
    pub carrier: u32,
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
    /// The city's stockpile, with supply on (STRATEGIC.md §4).
    pub bread: u32,
}

/// A resource site, with supply on (STRATEGIC.md §4). Sites have no owner:
/// any player's wagon standing on one can load its wheat.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Site {
    pub id: u32,
    pub hex: Hex,
    /// The pile of wheat waiting to be collected.
    pub wheat: u32,
    /// Wheat grown per round, and the most the pile holds.
    pub output: u32,
    pub cap: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    /// 1 or 2, or 0 for a draw.
    pub winner: u8,
    /// `elimination`, `cities`, `army`, or `draw`, or why a bot forfeited,
    /// such as `crash` or `hang`.
    pub reason: String,
}

/// Buys up to `want` rations at `cost` bread each from `stock`, and returns
/// how many it bought. Rations are free when `cost` is 0.
fn buy(stock: &mut u32, want: i64, cost: u32) -> i64 {
    if want <= 0 {
        return 0;
    }
    if cost == 0 {
        return want;
    }
    let n = (*stock / cost).min(want.min(u32::MAX as i64) as u32);
    *stock -= n * cost;
    n as i64
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
/// `def` with `defense` percent: its terrain's, plus any fortification
/// (SPEC §3.7). Every operand is non-negative, so kits get the same result
/// with floor division.
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
    /// Resource sites in id order. Only games with supply on have any.
    pub sites: Vec<Site>,
    /// Gold by player. Index 0 is unused. Always 0 with supply on, which
    /// pays for units with bread instead.
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
    /// The site on each hex, if any.
    site_at: Vec<Option<u32>>,
    /// `LOG` lines waiting for each player's next turn message.
    logs: [Vec<String>; 3],
    /// Whether `end_turn` has already run for the current turn. Callers may
    /// end a turn twice, and units must dig in only once.
    turn_over: bool,
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

    /// A game with the map's cities and sites but no units, for tests and
    /// scenarios. With supply on, each player's home city (their lowest
    /// numbered) starts with `start_bread`; otherwise each player starts
    /// with `start_gold`.
    pub fn empty(rules: Rules, map: Map) -> Game {
        let n = map.len();
        let supply = rules.supply();
        let mut cities: Vec<City> = map
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
                bread: 0,
            })
            .collect();
        if supply {
            for player in 1..=2u8 {
                if let Some(home) = cities.iter_mut().find(|c| c.owner == player) {
                    home.bread = rules.c.start_bread;
                }
            }
        }
        let sites: Vec<Site> = map
            .sites
            .iter()
            .enumerate()
            .map(|(i, s)| Site {
                id: i as u32,
                hex: s.hex,
                wheat: 0,
                output: s.output,
                cap: s.cap,
            })
            .collect();
        let mut city_at = vec![None; n];
        for c in &cities {
            city_at[map.index(c.hex)] = Some(c.id);
        }
        let mut site_at = vec![None; n];
        for s in &sites {
            site_at[map.index(s.hex)] = Some(s.id);
        }
        let start_gold = if supply { 0 } else { rules.c.start_gold };
        Game {
            rules,
            map,
            units: Vec::new(),
            cities,
            sites,
            gold: [0, start_gold, start_gold],
            round: 0,
            player: 0,
            next_unit_id: 1,
            outcome: None,
            visible: vec![false; n],
            occupants: vec![Vec::new(); n],
            city_at,
            site_at,
            logs: Default::default(),
            turn_over: false,
        }
    }

    /// Adds a full-HP unit with full rations and returns its id.
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
            fort: 0,
            attacked: false,
            rations: self.rules.c.ration_max,
            wheat: 0,
            bread: 0,
            carrier: 0,
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
        let naval = self.rules.units[ty].naval;
        if !self.map.contains(hex) || self.rules.terrains[self.map.terrain_at(hex)].cost_for(naval) == 0 {
            return Err(format!("unit {id}: {hex} isn't a hex on the map it can enter"));
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

    /// Adds a land unit aboard transport `carrier`, for scenarios. Ids must
    /// increase.
    pub fn place_aboard(&mut self, id: u32, ty: usize, carrier: u32, hp: u32) -> Result<(), String> {
        let Some(c) = self.unit(carrier).cloned() else {
            return Err(format!("unit {id}: there is no transport {carrier}"));
        };
        if !self.rules.units[c.ty].transport || ty >= self.rules.units.len() || self.rules.units[ty].naval {
            return Err(format!("unit {id}: only land units can board, and only transports"));
        }
        if self.aboard(carrier).len() as u32 >= self.rules.c.passengers_max {
            return Err(format!("unit {id}: transport {carrier} is full"));
        }
        if id < self.next_unit_id || hp == 0 || hp > self.rules.units[ty].max_hp {
            return Err(format!("unit {id}: ids must increase, and hp must fit the type"));
        }
        self.next_unit_id = id;
        self.spawn(c.owner, ty, c.hex, false);
        let u = self.units.last_mut().expect("just spawned");
        (u.hp, u.carrier) = (hp, carrier);
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

    /// The site on a hex on the map, if any.
    pub fn site_at(&self, hex: Hex) -> Option<&Site> {
        self.site_at[self.map.index(hex)].map(|s| &self.sites[s as usize])
    }

    /// The bread in a player's cities and carried by their units, with
    /// supply on.
    pub fn bread_of(&self, player: u8) -> u64 {
        let stored: u64 = self
            .cities
            .iter()
            .filter(|c| c.owner == player)
            .map(|c| c.bread as u64)
            .sum();
        let carried: u64 = self
            .units
            .iter()
            .filter(|u| u.owner == player)
            .map(|u| u.bread as u64)
            .sum();
        stored + carried
    }

    /// Σ cost × hp / max_hp over a player's units, which breaks ties among
    /// equal cities at the round limit. With supply on, where units cost
    /// bread, the bread in the player's cities and wagons counts too
    /// (STRATEGIC.md §9).
    pub fn army_value(&self, player: u8) -> u64 {
        let units: u64 = self
            .units
            .iter()
            .filter(|u| u.owner == player)
            .map(|u| {
                let t = &self.rules.units[u.ty];
                t.cost as u64 * u.hp as u64 / t.max_hp as u64
            })
            .sum();
        units + if self.rules.supply() { self.bread_of(player) } else { 0 }
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
    /// vision, in that order (SPEC §3.4). With supply on, production and
    /// feeding take the place of income (STRATEGIC.md §5). Player 1's turn
    /// starts a new round.
    pub fn begin_turn(&mut self, player: u8) {
        assert!(player == 1 || player == 2, "player must be 1 or 2");
        if player == 1 {
            self.round += 1;
        }
        self.player = player;
        self.turn_over = false;
        let p = player as usize;

        let mut starved = Vec::new();
        if self.rules.supply() {
            self.produce(player);
            starved = self.feed(player);
        } else {
            let cities = self.cities_of(player) as i64;
            let units = self.units_of(player) as i64;
            let c = &self.rules.c;
            let gold = self.gold[p] as i64 + cities * c.city_income as i64 - units * c.unit_upkeep as i64;
            self.gold[p] = gold.clamp(0, u32::MAX as i64) as u32;
        }

        for i in 0..self.units.len() {
            let u = &self.units[i];
            if u.owner != player || starved.binary_search(&u.id).is_ok() {
                continue;
            }
            // Land units heal on their own cities, and ships next to them
            // (STRATEGIC.md §6).
            let heals = if self.rules.units[u.ty].naval {
                u.hex.neighbors().into_iter().any(|h| self.own_city_at(h, player))
            } else {
                self.own_city_at(u.hex, player)
            };
            if heals {
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
                u.attacked = false;
            }
        }
        for c in self.cities.iter_mut() {
            c.built = false;
        }

        self.visible = self.vision(player);
    }

    /// At the start of each round, every site grows its wheat, whoever is
    /// near it. Then `player`'s cities make bread, except one with an enemy
    /// unit on or next to it (STRATEGIC.md §4).
    fn produce(&mut self, player: u8) {
        let enemy = 3 - player;
        if player == 1 {
            for s in self.sites.iter_mut() {
                if s.wheat < s.cap {
                    s.wheat = s.wheat.saturating_add(s.output).min(s.cap);
                }
            }
        }
        let made = self.rules.c.city_bread;
        for ci in 0..self.cities.len() {
            let hex = self.cities[ci].hex;
            let besieged = std::iter::once(hex)
                .chain(hex.neighbors())
                .any(|h| self.map.contains(h) && self.has_unit_of(h, enemy));
            if self.cities[ci].owner == player && !besieged {
                let city = &mut self.cities[ci];
                city.bread = city.bread.saturating_add(made);
            }
        }
    }

    /// Each of `player`'s units eats what its last turn cost, then refills to
    /// `ration_max` from its player's cities and wagons on or next to its hex:
    /// cities first, then wagons, each in id order, until it's full or they
    /// run out. A unit that can't cover what it ate starves (STRATEGIC.md §5).
    /// Returns the ids of the units that starved, in id order.
    fn feed(&mut self, player: u8) -> Vec<u32> {
        let c = &self.rules.c;
        let (max, cost, starve_hp) = (c.ration_max as i64, c.ration_cost, c.starve_hp);
        let (eat_base, eat_move, eat_attack) = (c.eat_base, c.eat_move, c.eat_attack);
        let ids: Vec<u32> = self.units.iter().filter(|u| u.owner == player).map(|u| u.id).collect();
        let mut starved = Vec::new();
        for id in ids {
            // A transport that starved to death sank with its passengers.
            let Some(i) = self.unit_idx(id) else {
                continue;
            };
            let u = &self.units[i];
            let eaten = eat_base + if u.moved { eat_move } else { 0 } + if u.attacked { eat_attack } else { 0 };
            let mut have = u.rations as i64 - eaten as i64;
            if have < max {
                let hex = u.hex;
                let mut cities = Vec::new();
                let mut wagons = Vec::new();
                for h in std::iter::once(hex).chain(hex.neighbors()) {
                    if !self.map.contains(h) {
                        continue;
                    }
                    let k = self.map.index(h);
                    if let Some(ci) = self.city_at[k].filter(|&ci| self.cities[ci as usize].owner == player) {
                        cities.push(ci as usize);
                    }
                    for &o in &self.occupants[k] {
                        let w = &self.units[self.unit_idx(o).expect("occupants are live units")];
                        if w.owner == player && self.rules.units[w.ty].carry {
                            wagons.push(o);
                        }
                    }
                }
                cities.sort_unstable();
                wagons.sort_unstable();
                for ci in cities {
                    have += buy(&mut self.cities[ci].bread, max - have, cost);
                }
                for w in wagons {
                    let wi = self.unit_idx(w).expect("wagons are live units");
                    have += buy(&mut self.units[wi].bread, max - have, cost);
                }
            }
            let u = &mut self.units[i];
            if have >= 0 {
                u.rations = have as u32;
            } else {
                starved.push(id);
                u.rations = 0;
                u.hp = u.hp.saturating_sub(starve_hp);
                if u.hp == 0 {
                    self.remove_unit(id);
                }
            }
        }
        starved
    }

    /// Ends the current turn: the player's `fortify` units that neither moved
    /// nor acted dig in one level deeper. After the last round, the tiebreak
    /// decides. Ending the same turn again changes nothing.
    pub fn end_turn(&mut self) {
        if !self.turn_over {
            self.turn_over = true;
            let max = self.rules.c.fortify_max;
            for u in self.units.iter_mut() {
                let still = !(u.moved || u.acted || u.fresh || u.carrier != 0);
                if u.owner == self.player && self.rules.units[u.ty].fortify && still {
                    u.fort = (u.fort + 1).min(max);
                }
            }
        }
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
    /// With supply on there's no `GOLD` line, `CITY` lines end with the
    /// city's bread, `SITE` lines follow them, and `UNIT` lines carry the
    /// supply fields (STRATEGIC.md §11).
    pub fn turn_message(&self, time_ms: u64, bank_ms: u64) -> Vec<String> {
        let p = self.player;
        let supply = self.rules.supply();
        let mut out = vec![format!("TURN {}", self.round), format!("TIME {time_ms} {bank_ms}")];
        if !supply {
            out.push(format!("GOLD {}", self.gold[p as usize]));
        }
        for c in &self.cities {
            let seen = self.visible[self.map.index(c.hex)];
            let (capturer, progress) = if seen { (c.capturer, c.progress) } else { (0, 0) };
            if supply {
                let bread = if seen { c.bread } else { 0 };
                out.push(format!("CITY {} {} {} {} {}", c.id, c.owner, capturer, progress, bread));
            } else {
                out.push(format!("CITY {} {} {} {}", c.id, c.owner, capturer, progress));
            }
        }
        // Sites have no owner or capture progress; the line keeps its fields,
        // always 0, so that kits read it as before.
        for s in &self.sites {
            let wheat = if self.visible[self.map.index(s.hex)] {
                s.wheat
            } else {
                0
            };
            out.push(format!("SITE {} 0 0 0 {}", s.id, wheat));
        }
        let mine = self.units.iter().filter(|u| u.owner == p);
        let seen = self
            .units
            .iter()
            .filter(|u| u.owner != p && self.visible[self.map.index(u.hex)]);
        for u in mine.chain(seen) {
            out.push(format!("UNIT {}", self.unit_fields(u)));
        }
        out.extend(self.logs[p as usize].iter().cloned());
        out.push("GO".into());
        out
    }

    /// A `UNIT` or `SEE` line after its keyword: id, owner, type, q, r, HP,
    /// and fortification level (SPEC §6.3), then with supply on the carrier,
    /// wheat, bread, and rations.
    fn unit_fields(&self, u: &Unit) -> String {
        let code = self.rules.units[u.ty].code;
        let base = format!(
            "{} {} {} {} {} {} {}",
            u.id, u.owner, code, u.hex.q, u.hex.r, u.hp, u.fort
        );
        if self.rules.supply() {
            format!("{base} {} {} {} {}", u.carrier, u.wheat, u.bread, u.rations)
        } else {
            base
        }
    }

    /// Runs one order line for the current player and returns the reply:
    /// zero or more `SEE` events, then one `OK`, `PART`, or `ERR` line.
    pub fn apply(&mut self, line: &str) -> Vec<String> {
        let mut out = Vec::new();
        let t: Vec<&str> = line.split_whitespace().collect();
        let supply = self.rules.supply();
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
                Some("L") if supply => self.order_load(&t, &mut out),
                Some("U") if supply => self.order_unload(&t, &mut out),
                Some(other) => {
                    let orders = if supply {
                        "M, A, C, B, L, U, D,"
                    } else {
                        "M, A, C, B, D,"
                    };
                    reject(
                        "syntax",
                        format!("unknown order '{other}'; orders are {orders} and END"),
                    )
                }
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
        if self.units[ui].carrier != 0 {
            return self.land(ui, &dirs, out);
        }
        let (naval, skirmish) = (self.rules.units[ty].naval, self.rules.units[ty].skirmish);

        // Check the whole path against what the player can see. A land unit
        // may end its path by boarding one of its player's transports, on a
        // hex it couldn't otherwise enter. The boarding step costs 1 MP.
        let mut pos = start;
        let mut cost = 0;
        let mut boarding = false;
        for (k, &dir) in dirs.iter().enumerate() {
            let next = pos.step(dir);
            if !self.map.contains(next) {
                return reject("path", format!("step {} leaves the map at {next}", k + 1));
            }
            let i = self.map.index(next);
            let terrain = &self.rules.terrains[self.map.tiles[i] as usize];
            let mut step = terrain.cost_for(naval);
            if step == 0 {
                if naval || k + 1 < dirs.len() || !self.transport_at(i) {
                    let who = if naval { "ships" } else { "land units" };
                    return reject("path", format!("{next} is {}, which {who} can't enter", terrain.name));
                }
                boarding = true;
                step = 1;
            }
            if self.visible[i] && self.enemy_at(i) {
                return reject("path", format!("{next} holds an enemy unit"));
            }
            cost += step;
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
        let transport = if boarding {
            match self.transport_with_room(dest) {
                Some(t) => t,
                None => return reject("stack_full", format!("the transports at {pos} are full")),
            }
        } else {
            let friends = self.stacked(dest, id);
            if friends >= self.rules.c.stack_limit {
                return reject("stack_full", format!("{pos} already holds {friends} units"));
            }
            0
        };

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
            let u = &mut self.units[ui];
            if transport != 0 && k + 1 == dirs.len() {
                // Boarding ends the unit's move.
                u.mp = 0;
                u.carrier = transport;
            } else {
                u.mp -= self.rules.terrains[self.map.tiles[i] as usize].cost_for(naval);
            }
            u.moved = true;
            u.fort = 0;
            self.relocate(ui, next);
            let radius = self.stack_vision(ui);
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

    /// A unit aboard a transport lands: a one-step move onto land next to
    /// it, which ends the unit's move but leaves its action, so it can still
    /// attack or capture (STRATEGIC.md §6).
    fn land(&mut self, ui: usize, dirs: &[usize], out: &mut Vec<String>) -> Result<(), Reject> {
        let (id, from, mp) = (self.units[ui].id, self.units[ui].hex, self.units[ui].mp);
        if dirs.len() != 1 {
            return reject(
                "path",
                format!("unit {id} is aboard a transport, so it can only land, one step onto land"),
            );
        }
        let next = from.step(dirs[0]);
        if !self.map.contains(next) {
            return reject("path", format!("step 1 leaves the map at {next}"));
        }
        let i = self.map.index(next);
        let terrain = &self.rules.terrains[self.map.tiles[i] as usize];
        if !terrain.passable() {
            return reject(
                "path",
                format!("{next} is {}, which land units can't enter", terrain.name),
            );
        }
        if self.visible[i] && self.enemy_at(i) {
            return reject("path", format!("{next} holds an enemy unit"));
        }
        if terrain.move_cost > mp {
            return reject(
                "no_mp",
                format!("the path costs {} MP but unit {id} has {mp}", terrain.move_cost),
            );
        }
        let friends = self.stacked(i, id);
        if friends >= self.rules.c.stack_limit {
            return reject("stack_full", format!("{next} already holds {friends} units"));
        }
        if self.enemy_at(i) {
            self.units[ui].mp = 0;
            self.reveal(next, 0, out);
            out.push(format!("PART {} {} ambush", from.q, from.r));
            return Ok(());
        }
        let u = &mut self.units[ui];
        (u.mp, u.carrier, u.moved, u.fort) = (0, 0, true, 0);
        self.relocate(ui, next);
        let radius = self.unit_vision(ui);
        self.reveal(next, radius, out);
        out.push(format!("OK {} {} 0", next.q, next.r));
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
        self.check_ashore(ai, "attack")?;
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
        if self.units[di].carrier != 0 {
            return reject(
                "aboard",
                format!(
                    "unit {target} is aboard transport {}; only the transport can be attacked",
                    self.units[di].carrier
                ),
            );
        }
        if self.rules.damage[aty][dty] == 0 {
            return reject(
                "no_damage",
                format!("{}s can't hurt {}s", at.name, self.rules.units[dty].name),
            );
        }
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

        let d_hp = d_hp.saturating_sub(damage(&self.rules, aty, a_hp, dty, self.defense(di)));
        let mut a_hp = a_hp;
        if d_hp > 0 && self.rules.units[dty].in_range(dist) && self.rules.damage[dty][aty] > 0 {
            a_hp = a_hp.saturating_sub(damage(&self.rules, dty, d_hp, aty, self.defense(ai)));
        }

        let attacker = &mut self.units[ai];
        attacker.hp = a_hp;
        attacker.acted = true;
        attacker.attacked = true;
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
        self.check_ashore(ui, "capture")?;
        let (hex, hp, ty) = (self.units[ui].hex, self.units[ui].hp, self.units[ui].ty);
        if !self.rules.units[ty].capture {
            return reject(
                "cannot_capture",
                format!("{}s can't capture cities", self.rules.units[ty].name),
            );
        }
        let at = self.map.index(hex);
        if let Some(si) = self.site_at[at] {
            return reject(
                "cannot_capture",
                format!("site {si} has no owner to take it from: any wagon on it can load its wheat"),
            );
        }
        let Some(ci) = self.city_at[at].map(|c| c as usize) else {
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
        let at = if self.rules.units[ty].naval {
            self.launch_hex(ci)?
        } else {
            let here = self.stacked(i, 0);
            if here >= self.rules.c.stack_limit {
                return reject("stack_full", format!("city {ci} already holds {here} units"));
            }
            hex
        };
        let (cost, name) = (self.rules.units[ty].cost, &self.rules.units[ty].name);
        let p = self.player as usize;
        if self.rules.supply() {
            // Each city pays from its own stockpile (STRATEGIC.md §4).
            let bread = self.cities[ci].bread;
            if bread < cost {
                return reject(
                    "no_bread",
                    format!("a {name} costs {cost} bread and city {ci} has {bread}"),
                );
            }
            self.cities[ci].bread -= cost;
        } else {
            if self.gold[p] < cost {
                return reject(
                    "no_gold",
                    format!("a {name} costs {cost} gold and you have {}", self.gold[p]),
                );
            }
            self.gold[p] -= cost;
        }

        self.cities[ci].built = true;
        let id = self.spawn(self.player, ty, at, true);
        let radius = self.unit_vision(self.units.len() - 1);
        self.reveal(at, radius, out);
        out.push(format!("OK {id}"));
        Ok(())
    }

    /// Where city `ci` puts a new ship: the first hex next to it, in
    /// direction order, that ships can enter, with room and no enemy units
    /// (STRATEGIC.md §6). A city with such hexes next to it is a port.
    fn launch_hex(&self, ci: usize) -> Result<Hex, Reject> {
        let mut port = false;
        for h in self.cities[ci].hex.neighbors() {
            if !self.map.contains(h) {
                continue;
            }
            let i = self.map.index(h);
            if self.rules.terrains[self.map.tiles[i] as usize].naval_move_cost == 0 {
                continue;
            }
            port = true;
            if !self.enemy_at(i) && self.stacked(i, 0) < self.rules.c.stack_limit {
                return Ok(h);
            }
        }
        if port {
            reject("stack_full", format!("no water next to city {ci} has room for a ship"))
        } else {
            reject(
                "no_port",
                format!("city {ci} isn't next to water, so it can't build ships"),
            )
        }
    }

    /// The unit and amount in an `L` or `U` order, after the checks both share.
    fn carrier_order(&self, t: &[&str], usage: &str) -> Result<(usize, u32), Reject> {
        if t.len() != 2 && t.len() != 3 {
            return reject("syntax", usage);
        }
        let id = number(t[1], "unit id")?;
        let limit = match t.get(2) {
            Some(tok) => number(tok, "amount")?,
            None => u32::MAX,
        };
        let ui = self.own_unit(id)?;
        self.check_ready(ui)?;
        self.check_ashore(ui, "load or unload")?;
        let ty = &self.rules.units[self.units[ui].ty];
        if !ty.carry {
            return reject("cannot_carry", format!("{}s can't carry goods", ty.name));
        }
        Ok((ui, limit))
    }

    /// `L <unit> [amount]`: a wagon takes wheat from the site it stands on,
    /// any site, or bread from the city, which must be its player's, as much
    /// as fits (STRATEGIC.md §4).
    fn order_load(&mut self, t: &[&str], out: &mut Vec<String>) -> Result<(), Reject> {
        let (ui, limit) = self.carrier_order(t, "usage: L <unit> [amount]")?;
        let (id, hex) = (self.units[ui].id, self.units[ui].hex);
        let at = self.map.index(hex);
        let player = self.player;
        let u = &self.units[ui];
        let room = self.rules.c.cargo_max.saturating_sub(u.wheat.saturating_add(u.bread));
        let left = if let Some(ci) = self.city_at[at]
            .map(|c| c as usize)
            .filter(|&c| self.cities[c].owner == player)
        {
            let n = self.cities[ci].bread.min(room).min(limit);
            self.cities[ci].bread -= n;
            self.units[ui].bread += n;
            self.cities[ci].bread
        } else if let Some(si) = self.site_at[at].map(|s| s as usize) {
            let n = self.sites[si].wheat.min(room).min(limit);
            self.sites[si].wheat -= n;
            self.units[ui].wheat += n;
            self.sites[si].wheat
        } else {
            return reject("cannot_load", format!("unit {id} isn't on a site or a city you hold"));
        };
        let u = &mut self.units[ui];
        u.mp = 0;
        out.push(format!("OK {} {} {left}", u.wheat, u.bread));
        Ok(())
    }

    /// `U <unit> [amount]`: a wagon puts its wheat, then its bread, into the
    /// city it stands on. The wheat becomes bread (STRATEGIC.md §4).
    fn order_unload(&mut self, t: &[&str], out: &mut Vec<String>) -> Result<(), Reject> {
        let (ui, limit) = self.carrier_order(t, "usage: U <unit> [amount]")?;
        let (id, hex) = (self.units[ui].id, self.units[ui].hex);
        let player = self.player;
        let Some(ci) = self.city_at[self.map.index(hex)]
            .map(|c| c as usize)
            .filter(|&c| self.cities[c].owner == player)
        else {
            return reject("cannot_unload", format!("unit {id} isn't on a city you hold"));
        };
        let u = &mut self.units[ui];
        let wheat = u.wheat.min(limit);
        let bread = u.bread.min(limit - wheat);
        u.wheat -= wheat;
        u.bread -= bread;
        u.mp = 0;
        let city = &mut self.cities[ci];
        city.bread = city.bread.saturating_add(wheat + bread);
        out.push(format!("OK {} {} {}", u.wheat, u.bread, city.bread));
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

    /// Rejects an order a unit aboard a transport can't carry out.
    fn check_ashore(&self, i: usize, what: &str) -> Result<(), Reject> {
        let u = &self.units[i];
        if u.carrier != 0 {
            return reject(
                "aboard",
                format!("unit {} is aboard transport {} and can't {what}", u.id, u.carrier),
            );
        }
        Ok(())
    }

    /// Whether `player` owns a city on the map hex `hex`.
    fn own_city_at(&self, hex: Hex, player: u8) -> bool {
        self.map.contains(hex)
            && self.city_at[self.map.index(hex)].is_some_and(|ci| self.cities[ci as usize].owner == player)
    }

    /// Ids of the units aboard transport `id`, in occupant order.
    pub fn aboard(&self, id: u32) -> Vec<u32> {
        let Some(t) = self.unit(id) else {
            return Vec::new();
        };
        self.occupants[self.map.index(t.hex)]
            .iter()
            .copied()
            .filter(|&o| self.units[self.unit_idx(o).expect("occupants are live units")].carrier == id)
            .collect()
    }

    /// How many of the current player's units on the hex at storage index
    /// `i` count toward the stack limit: all but those aboard transports,
    /// and but unit `except`.
    fn stacked(&self, i: usize, except: u32) -> u32 {
        self.occupants[i]
            .iter()
            .filter(|&&o| {
                let u = &self.units[self.unit_idx(o).expect("occupants are live units")];
                o != except && u.owner == self.player && u.carrier == 0
            })
            .count() as u32
    }

    /// Whether the hex at storage index `i` holds one of the current
    /// player's transports.
    fn transport_at(&self, i: usize) -> bool {
        self.occupants[i].iter().any(|&o| {
            let u = &self.units[self.unit_idx(o).expect("occupants are live units")];
            u.owner == self.player && self.rules.units[u.ty].transport
        })
    }

    /// The current player's lowest-numbered transport with room on the hex
    /// at storage index `i`.
    fn transport_with_room(&self, i: usize) -> Option<u32> {
        let max = self.rules.c.passengers_max as usize;
        let mut ids: Vec<u32> = self.occupants[i].clone();
        ids.sort_unstable();
        ids.into_iter().find(|&o| {
            let u = &self.units[self.unit_idx(o).expect("occupants are live units")];
            u.owner == self.player && self.rules.units[u.ty].transport && self.aboard(o).len() < max
        })
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
                    u.owner != self.player && u.carrier == 0 && self.rules.units[u.ty].zoc
                })
        })
    }

    /// The percent of damage the unit at index `i` blocks: its hex's terrain
    /// defense plus `fortify_pct` for each fortification level (SPEC §3.7).
    /// `damage` caps it at 100.
    fn defense(&self, i: usize) -> u32 {
        let u = &self.units[i];
        let fort = u.fort.saturating_mul(self.rules.c.fortify_pct);
        self.rules.terrains[self.map.terrain_at(u.hex)]
            .defense
            .saturating_add(fort)
    }

    fn unit_vision(&self, i: usize) -> u32 {
        let u = &self.units[i];
        self.rules.units[u.ty].vision + self.rules.terrains[self.map.terrain_at(u.hex)].vision_bonus
    }

    /// The vision of unit `i` together with the units aboard it, which all
    /// see from the same hex.
    fn stack_vision(&self, i: usize) -> u32 {
        let aboard = self.aboard(self.units[i].id);
        aboard
            .into_iter()
            .map(|o| self.unit_vision(self.unit_idx(o).expect("passengers are live units")))
            .fold(self.unit_vision(i), u32::max)
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
                out.push(format!("SEE {}", self.unit_fields(u)));
            }
        }
    }

    /// Moves unit `i` to `to`, with everything aboard it.
    fn relocate(&mut self, i: usize, to: Hex) {
        let id = self.units[i].id;
        let from = self.map.index(self.units[i].hex);
        let mut moving = self.aboard(id);
        moving.insert(0, id);
        self.occupants[from].retain(|o| !moving.contains(o));
        let dest = self.map.index(to);
        for o in moving {
            self.occupants[dest].push(o);
            let k = self.unit_idx(o).expect("moving units are live");
            self.units[k].hex = to;
        }
    }

    /// Removes a unit. A transport sinks with everything aboard it.
    fn remove_unit(&mut self, id: u32) {
        let aboard = self.aboard(id);
        if let Some(i) = self.unit_idx(id) {
            let u = self.units.remove(i);
            let at = self.map.index(u.hex);
            self.occupants[at].retain(|&o| o != id);
        }
        for o in aboard {
            self.remove_unit(o);
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
            if u.rations > self.rules.c.ration_max {
                return Err(format!("unit {} has {} rations", u.id, u.rations));
            }
            let cargo = u.wheat as u64 + u.bread as u64;
            if cargo > if t.carry { self.rules.c.cargo_max as u64 } else { 0 } {
                return Err(format!("unit {} carries {} wheat and {} bread", u.id, u.wheat, u.bread));
            }
            let terrain = &self.rules.terrains[self.map.terrain_at(u.hex)];
            if u.carrier != 0 {
                let ok = self.unit(u.carrier).is_some_and(|c| {
                    c.owner == u.owner && c.hex == u.hex && c.carrier == 0 && self.rules.units[c.ty].transport
                });
                if !ok || t.naval {
                    return Err(format!("unit {} is aboard {}, which can't carry it", u.id, u.carrier));
                }
            } else if terrain.cost_for(t.naval) == 0 {
                return Err(format!("unit {} stands on {}", u.id, terrain.name));
            }
            if t.transport && self.aboard(u.id).len() as u32 > self.rules.c.passengers_max {
                return Err(format!("transport {} carries too many units", u.id));
            }
        }
        for s in &self.sites {
            if s.wheat > s.cap {
                return Err(format!("site {} holds {} wheat, over its cap", s.id, s.wheat));
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

    /// The default rules with supply on, a site terrain, and wagons.
    fn supply_rules() -> Rules {
        let mut r = Rules::default();
        r.set("supply", "1").unwrap();
        r.set("terrain.R", "site 1 0 0 0").unwrap();
        r.set("unit.W", "wagon 100 100 4 2 1 1 carry").unwrap();
        r
    }

    #[test]
    fn sites_grow_once_a_round_for_anyone_and_bread_counts_in_the_army() {
        let lines = "MAP 6 4\nROW C.....\nROW .CRC..\nROW ......\nROW .....C\n\
                     CITY 0 0 0 1\nCITY 1 3 1 1\nCITY 2 1 1 1\nCITY 3 4 3 2\nSITE 0 2 1 0 50 1000";
        let (rules, map) = parse_setup(lines.lines(), supply_rules()).unwrap();
        let mut g = Game::empty(rules, map);
        assert_eq!(g.gold, [0, 0, 0]);
        // The round starts: the site grows, and keeps its wheat though two of
        // player 1's cities are next to it. Each city makes its own bread.
        g.begin_turn(1);
        let bread: Vec<u32> = g.cities.iter().map(|c| c.bread).collect();
        assert_eq!(bread, vec![310, 10, 10, 300]);
        assert_eq!(g.sites[0].wheat, 50);
        assert!(!g.turn_message(0, 0).iter().any(|l| l.starts_with("GOLD")));

        // It grows once a round, not on player 2's turn too.
        g.cities[2].owner = 2;
        g.end_turn();
        g.begin_turn(2);
        assert_eq!(g.sites[0].wheat, 50);

        // Two cities each, so the army decides, bread included: player 2
        // holds 310 + 20, and player 1 310 + 10.
        assert_eq!((g.army_value(1), g.army_value(2)), (320, 330));
        g.round = g.rules.c.round_limit;
        g.end_turn();
        let outcome = g.outcome.clone().unwrap();
        assert_eq!((outcome.winner, outcome.reason.as_str()), (2, "army"));
    }

    #[test]
    fn any_wagon_loads_at_any_site_and_no_one_captures_one() {
        let lines = "MAP 6 4\nROW C.....\nROW .CRC..\nROW ......\nROW .....C\n\
                     CITY 0 0 0 1\nCITY 1 3 1 1\nCITY 2 1 1 1\nCITY 3 4 3 2\nSITE 0 2 1 0 50 1000";
        let (rules, map) = parse_setup(lines.lines(), supply_rules()).unwrap();
        let mut g = Game::empty(rules, map);
        let (wagon, infantry) = (g.rules.unit_index('W').unwrap(), g.rules.unit_index('I').unwrap());
        g.place(1, 2, wagon, Hex::new(2, 1), 100).unwrap();
        g.place(2, 2, infantry, Hex::new(2, 1), 100).unwrap();
        g.begin_turn(1);
        g.end_turn();
        g.begin_turn(2);
        assert_eq!(
            g.apply("C 2")[0].split(' ').take(2).collect::<Vec<_>>(),
            ["ERR", "cannot_capture"]
        );
        assert_eq!(g.apply("L 1 30"), ["OK 30 0 20"]);
        assert!(g.turn_message(0, 0).contains(&"SITE 0 0 0 0 20".to_string()));
        assert!(parse_setup(lines.replace("SITE 0 2 1 0", "SITE 0 2 1 2").lines(), supply_rules()).is_err());
    }

    #[test]
    fn fortify_units_dig_in_while_they_hold_still() {
        let mut rules = Rules::default();
        rules.set("unit.P.flags", "capture,zoc,fortify").unwrap();
        rules.set("fortify_max", "3").unwrap();
        let map = crate::mapgen::generate(&rules, 7).unwrap();
        let mut g = Game::empty(rules, map);
        let (pike, infantry) = (g.rules.unit_index('P').unwrap(), g.rules.unit_index('I').unwrap());
        let h1 = g.cities.iter().find(|c| c.owner == 1).unwrap().hex;
        let h2 = g.cities.iter().find(|c| c.owner == 2).unwrap().hex;
        let idle = g.spawn(1, pike, h1, false);
        let busy = g.spawn(1, pike, h1, false);
        let foot = g.spawn(1, infantry, h1, false);
        let theirs = g.spawn(2, pike, h2, false);
        let levels = |g: &Game| [idle, busy, foot, theirs].map(|id| g.unit(id).unwrap().fort);

        g.begin_turn(1);
        g.end_turn();
        g.end_turn(); // ending a turn twice digs in only once
        assert_eq!(levels(&g), [1, 1, 0, 0]);
        g.begin_turn(2);
        g.end_turn();
        assert_eq!(levels(&g), [1, 1, 0, 1]);

        g.begin_turn(1);
        let i = g.unit_idx(busy).unwrap();
        g.units[i].acted = true; // as if it attacked: it keeps its level but gains none
        let new = g.spawn(1, pike, h1, true);
        g.end_turn();
        assert_eq!(levels(&g), [2, 1, 0, 1]);
        assert_eq!(g.unit(new).unwrap().fort, 0);
        g.begin_turn(2);
        g.end_turn();

        for _ in 0..2 {
            g.begin_turn(1);
            g.end_turn();
            g.begin_turn(2);
            g.end_turn();
        }
        assert_eq!(levels(&g), [3, 3, 0, 3]);
        assert_eq!(g.unit(new).unwrap().fort, 2);
    }
}
