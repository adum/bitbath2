// Forager: a simple bot in Rust, built for the strategic ruleset
// (STRATEGIC.md) but able to play the default game too. Where the
// reference bot masses its army on one enemy city, Forager spreads out and
// goes after the enemy's supply:
//
// - Every capturer heads for the nearest city or site no other unit is
//   after, so it takes ground fast.
// - Cavalry and scouts raid: they hunt enemy wagons and stand on enemy
//   sites, which stops them growing wheat, or on enemy cities, which stops
//   them building. Without supply there's nothing to raid but cities, so
//   only scouts raid, and cavalry fight with the rest.
// - Units that aren't capturing or raiding hunt the enemy units in sight.
// - Pikemen hold the cities nearest the enemy and dig in.
// - A unit attacks only when it takes more value than it loses back, with
//   no planned kills.
// - Wagons haul wheat from the sites that don't deliver by themselves, one
//   wagon in three carries bread to hungry units, and hungry units walk to
//   bread.
// - It builds a fixed mix of unit types with each city's own bread (or with
//   gold in the default game), and no more units than its bread can feed.
//
// With ships (STRATEGIC.md §6) it goes overseas the same way, spread out:
//
// - Each transport sails on its own, not in convoys. It fills up at a port
//   with units that have nothing left to take on their landmass, sails for
//   the best city or site across the water, preferring ones no other
//   transport is after, puts everyone ashore, and comes back for more.
// - On a landmass where it holds sites but no city, the units that took
//   them stand guard, and a transport ferries wagons between the sites and
//   a port, taking bread out for the guards and bringing wheat home.
// - Galleys raid at sea: they sink enemy transports and ferries, and
//   otherwise lie off the enemy's ports.
// - A home island of 4 cities or fewer keeps 2 land units for each city.
//
// It has no dependencies. Build: rustc -O bots/forager_bot.rs -o target/release/forager_bot.exe

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::time::Instant;

const DIRS: [(i32, i32); 6] = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)];
const FAR: i32 = 1 << 28;
// A hex that's in no landmass, or no sea.
const NONE: u32 = u32::MAX;
// Stop sending orders when this little of the turn is left.
const MARGIN_MS: u128 = 120;
// The army it builds toward, as shares by unit code. Wagons are built by need.
// Scouts raid sites in supply games; without supply they'd only be weak fighters.
const MIX: [(char, f64); 5] = [('I', 0.40), ('P', 0.20), ('K', 0.15), ('A', 0.10), ('S', 0.15)];
const MIX_NO_SUPPLY: [(char, f64); 5] = [('I', 0.45), ('P', 0.20), ('K', 0.20), ('A', 0.15), ('S', 0.0)];
// Bread a city keeps back to feed the units around it.
const KEEP: i64 = 50;
// A capturer goes for a city or site at most this many turns away.
const TARGET_TURNS: i32 = 15;
// A wagon fetches wheat once a site holds this much (or all it can hold).
const LOAD: i64 = 200;
// Ships: the most transports and galleys it keeps.
const TRANSPORTS_MAX: usize = 4;
const GALLEYS_MAX: usize = 3;
// A docked transport with units aboard sails after this many turns, full or not.
const SAIL_WAIT: i32 = 3;
// On a home island of at most HOME_ISLAND cities, GARRISON land units for
// each city stay home when transports sail.
const GARRISON: i64 = 2;
const HOME_ISLAND: usize = 4;
// A ferry waits at the island this many turns for its wagons to fill up.
const FERRY_WAIT: i32 = 6;
// Bread a ferry's first wagon takes out for each guard on the island.
const GUARD_BREAD: i64 = 60;
// A galley with fewer rations than this goes home to eat.
const SHIP_RATIONS: i32 = 16;

fn dist(a: (i32, i32), b: (i32, i32)) -> i32 {
    let (dq, dr) = (a.0 - b.0, a.1 - b.1);
    (dq.abs() + dr.abs() + (dq + dr).abs()) / 2
}

struct Terrain {
    move_cost: i32,
    defense: i32,
    // For ships; 0 where they can't go.
    naval: i32,
}

struct UnitType {
    code: char,
    cost: i64,
    max_hp: i32,
    mv: i32,
    range: (i32, i32),
    capture: bool,
    zoc: bool,
    skirmish: bool,
    no_move_attack: bool,
    carry: bool,
    naval: bool,
    transport: bool,
}

#[derive(Clone)]
struct Unit {
    id: u32,
    owner: u8,
    ty: usize,
    at: usize,
    hp: i32,
    fort: i32,
    // The transport it's aboard, or 0.
    carrier: u32,
    wheat: i64,
    bread: i64,
    rations: i32,
    mp: i32,
    acted: bool,
    moved: bool,
    fresh: bool,
    alive: bool,
    busy: bool,
}

#[derive(Clone, Copy, PartialEq)]
enum Phase {
    // At a port, taking units or wagons on.
    Dock,
    // Sailing to put them ashore.
    Out,
    // A ferry waiting at the island while its wagons load wheat.
    Wait,
    // Sailing home.
    Back,
}

/// A transport's plan.
#[derive(Clone)]
struct Ship {
    phase: Phase,
    port: Option<usize>,
    // What its units sail for: a city (true) or a site (false), by index.
    target: Option<(bool, usize)>,
    // A ferry's landmass.
    isle: u32,
    ferry: bool,
    waited: i32,
}

struct Place {
    at: usize,
    owner: u8,
    progress: i32,
    capturer: u8,
    stock: i64, // a city's bread, or a site's wheat
    built: bool,
    output: i64,
    cap: i64,
}

struct Bot {
    input: BufReader<io::Stdin>,
    output: BufWriter<io::Stdout>,
    me: u8,
    w: i32,
    h: i32,
    tiles: Vec<usize>,
    terrains: Vec<Terrain>,
    tchar: HashMap<char, usize>,
    types: Vec<UnitType>,
    damage: Vec<Vec<i32>>,
    c: HashMap<String, i64>,
    supply: bool,
    cities: Vec<Place>,
    sites: Vec<Place>,
    city_field: Vec<Vec<i32>>,
    site_field: Vec<Vec<i32>>,
    // Ships.
    naval: bool,
    pmax: i64,
    transport: Option<usize>,
    galley: Option<usize>,
    land: Vec<u32>,
    sea: Vec<u32>,
    home_isles: Vec<u32>,
    landing_cache: HashMap<usize, Vec<usize>>,
    sea_cache: HashMap<Vec<usize>, Vec<i32>>,
    ships: HashMap<u32, Ship>,
    crew: HashMap<u32, u32>,    // land unit id -> the transport it's to board
    haulers: HashMap<u32, u32>, // wagon id -> the landmass a ferry hauls its wheat from
    seen_ships: HashMap<u32, i64>,
    want_t: usize,
    want_g: usize,
    useful: Vec<u32>, // seas worth building ships on
    // This turn.
    round: i64,
    time_ms: u128,
    start: Instant,
    gold: i64,
    units: Vec<Unit>,
    friends: Vec<u8>,
    enemy: Vec<bool>,
    zoc: Vec<bool>,
    food: Vec<i32>,
    jobs: HashMap<u32, usize>, // wagon id -> site it hauls from
}

impl Bot {
    fn new() -> Bot {
        Bot {
            input: BufReader::new(io::stdin()),
            output: BufWriter::new(io::stdout()),
            me: 1,
            w: 0,
            h: 0,
            tiles: Vec::new(),
            terrains: Vec::new(),
            tchar: HashMap::new(),
            types: Vec::new(),
            damage: Vec::new(),
            c: HashMap::new(),
            supply: false,
            cities: Vec::new(),
            sites: Vec::new(),
            city_field: Vec::new(),
            site_field: Vec::new(),
            naval: false,
            pmax: 0,
            transport: None,
            galley: None,
            land: Vec::new(),
            sea: Vec::new(),
            home_isles: Vec::new(),
            landing_cache: HashMap::new(),
            sea_cache: HashMap::new(),
            ships: HashMap::new(),
            crew: HashMap::new(),
            haulers: HashMap::new(),
            seen_ships: HashMap::new(),
            want_t: 0,
            want_g: 0,
            useful: Vec::new(),
            round: 0,
            time_ms: 0,
            start: Instant::now(),
            gold: 0,
            units: Vec::new(),
            friends: Vec::new(),
            enemy: Vec::new(),
            zoc: Vec::new(),
            food: Vec::new(),
            jobs: HashMap::new(),
        }
    }

    // Protocol.

    fn read(&mut self) -> Option<String> {
        let mut line = String::new();
        match self.input.read_line(&mut line) {
            Ok(0) | Err(_) => None,
            Ok(_) => Some(line.trim_end().to_string()),
        }
    }

    fn send(&mut self, line: &str) {
        let _ = writeln!(self.output, "{line}");
        let _ = self.output.flush();
    }

    fn time_left(&self) -> bool {
        self.start.elapsed().as_millis() + MARGIN_MS < self.time_ms
    }

    /// Sends an order and returns its result line's tokens, applying SEE events.
    fn order(&mut self, line: &str) -> Vec<String> {
        self.send(line);
        loop {
            let Some(reply) = self.read() else {
                std::process::exit(0)
            };
            let t: Vec<String> = reply.split_whitespace().map(String::from).collect();
            match t.first().map(String::as_str) {
                Some("SEE") => {
                    let u = self.parse_unit(&t);
                    self.add_enemy(u);
                }
                Some("OK") | Some("PART") | Some("ERR") => return t,
                Some("GAMEOVER") => std::process::exit(0),
                _ => {}
            }
        }
    }

    fn rule(&self, key: &str) -> i64 {
        *self.c.get(key).unwrap_or(&0)
    }

    fn init(&mut self) {
        let mut rows: Vec<String> = Vec::new();
        let mut cities = Vec::new();
        let mut sites = Vec::new();
        while let Some(line) = self.read() {
            let t: Vec<&str> = line.split_whitespace().collect();
            let num = |i: usize| t.get(i).and_then(|s| s.parse::<i64>().ok()).unwrap_or(0);
            match t.first().copied() {
                Some("INIT") => break,
                Some("PLAYER") => self.me = num(1) as u8,
                Some("MAP") => (self.w, self.h) = (num(1) as i32, num(2) as i32),
                Some("ROW") => rows.push(t.get(1).unwrap_or(&"").to_string()),
                Some("CITY") => cities.push((num(2) as i32, num(3) as i32, num(4) as u8, 0, 0)),
                Some("SITE") => sites.push((num(2) as i32, num(3) as i32, num(4) as u8, num(5), num(6))),
                Some("TERRAIN") => {
                    let ch = t[1].chars().next().unwrap_or('?');
                    self.tchar.insert(ch, self.terrains.len());
                    self.terrains.push(Terrain {
                        move_cost: num(3) as i32,
                        defense: num(4) as i32,
                        naval: num(7) as i32,
                    });
                }
                Some("UNITTYPE") => {
                    let flags = t.get(9).copied().unwrap_or("-");
                    let has = |f: &str| flags.split(',').any(|x| x == f);
                    self.types.push(UnitType {
                        code: t[1].chars().next().unwrap_or('?'),
                        cost: num(3),
                        max_hp: num(4) as i32,
                        mv: num(5) as i32,
                        range: (num(7) as i32, num(8) as i32),
                        capture: has("capture"),
                        zoc: has("zoc"),
                        skirmish: has("skirmish"),
                        no_move_attack: has("no_move_attack"),
                        carry: has("carry"),
                        naval: has("naval"),
                        transport: has("transport"),
                    });
                }
                Some("DAMAGE") => self
                    .damage
                    .push(t[2..].iter().map(|s| s.parse().unwrap_or(0)).collect()),
                Some("RULE") => {
                    self.c.insert(t[1].to_string(), num(2));
                }
                _ => {}
            }
        }
        // DAMAGE lines come in UNITTYPE order.
        self.supply = self.rule("supply") == 1;
        self.tiles = rows
            .iter()
            .flat_map(|row| {
                row.chars()
                    .map(|ch| *self.tchar.get(&ch).unwrap_or(&0))
                    .collect::<Vec<_>>()
            })
            .collect();
        let place = |s: &Bot, q: i32, r: i32, owner: u8, output: i64, cap: i64| Place {
            at: s.idx(q, r).unwrap_or(0),
            owner,
            progress: 0,
            capturer: 0,
            stock: 0,
            built: false,
            output,
            cap,
        };
        self.cities = cities
            .iter()
            .map(|&(q, r, o, a, b)| place(self, q, r, o, a, b))
            .collect();
        self.sites = sites
            .iter()
            .map(|&(q, r, o, a, b)| place(self, q, r, o, a, b))
            .collect();
        self.city_field = (0..self.cities.len())
            .map(|i| self.field(&[self.cities[i].at]))
            .collect();
        self.site_field = (0..self.sites.len()).map(|i| self.field(&[self.sites[i].at])).collect();
        // Landmasses and seas, and the ships, if the rules have any.
        self.land = self.areas(|s, i| s.cost(i) > 0);
        self.sea = self.areas(|s, i| s.naval_cost(i) > 0);
        self.pmax = self.rule("passengers_max");
        self.transport = self.types.iter().position(|t| t.transport);
        self.galley = (0..self.types.len())
            .find(|&k| self.types[k].naval && !self.types[k].transport && self.damage[k].iter().any(|&d| d > 0));
        self.naval = self.supply && self.transport.is_some() && self.terrains.iter().any(|t| t.naval > 0);
        for &(q, r, owner, _, _) in &cities {
            let Some(at) = self.idx(q, r) else { continue };
            let isle = self.land[at];
            let size = self.cities.iter().filter(|c| self.land[c.at] == isle).count();
            if owner == self.me && size <= HOME_ISLAND {
                self.home_isles.push(isle);
            }
        }
        self.send("READY");
    }

    fn parse_unit(&self, t: &[String]) -> Unit {
        let num = |i: usize| t.get(i).and_then(|s| s.parse::<i64>().ok()).unwrap_or(0);
        let code = t[3].chars().next().unwrap_or('?');
        let ty = self.types.iter().position(|u| u.code == code).unwrap_or(0);
        Unit {
            id: num(1) as u32,
            owner: num(2) as u8,
            ty,
            at: self.idx(num(4) as i32, num(5) as i32).unwrap_or(0),
            hp: num(6) as i32,
            fort: num(7) as i32,
            carrier: num(8) as u32,
            wheat: num(9),
            bread: num(10),
            rations: num(11) as i32,
            mp: self.types[ty].mv,
            acted: false,
            moved: false,
            fresh: false,
            alive: true,
            busy: false,
        }
    }

    fn add_enemy(&mut self, u: Unit) {
        if let Some(old) = self.units.iter_mut().find(|v| v.id == u.id) {
            *old = u;
        } else {
            self.units.push(u);
        }
        self.mark();
    }

    /// Reads a turn message after its TURN line. Returns false at the end of the game.
    fn read_turn(&mut self) {
        self.start = Instant::now();
        self.units.clear();
        while let Some(line) = self.read() {
            let t: Vec<String> = line.split_whitespace().map(String::from).collect();
            let num = |i: usize| t.get(i).and_then(|s| s.parse::<i64>().ok()).unwrap_or(0);
            match t.first().map(String::as_str) {
                Some("GO") => break,
                Some("TIME") => self.time_ms = num(1) as u128,
                Some("GOLD") => self.gold = num(1),
                Some("CITY") => {
                    let c = &mut self.cities[num(1) as usize];
                    (c.owner, c.capturer, c.progress, c.stock, c.built) =
                        (num(2) as u8, num(3) as u8, num(4) as i32, num(5), false);
                }
                Some("SITE") => {
                    let s = &mut self.sites[num(1) as usize];
                    (s.owner, s.capturer, s.progress, s.stock) = (num(2) as u8, num(3) as u8, num(4) as i32, num(5));
                }
                Some("UNIT") => {
                    let u = self.parse_unit(&t);
                    self.units.push(u);
                }
                _ => {}
            }
        }
        // Collected first, so the closure doesn't borrow all of self, which
        // editions before 2021, plain rustc's default, would refuse.
        let ids: Vec<u32> = self.units.iter().map(|u| u.id).collect();
        self.jobs.retain(|id, _| ids.contains(id));
        self.mark();
    }

    // The map.

    fn idx(&self, q: i32, r: i32) -> Option<usize> {
        if r < 0 || r >= self.h {
            return None;
        }
        let col = q + r / 2;
        if col < 0 || col >= self.w {
            None
        } else {
            Some((r * self.w + col) as usize)
        }
    }

    fn qr(&self, i: usize) -> (i32, i32) {
        let (r, col) = (i as i32 / self.w, i as i32 % self.w);
        (col - r / 2, r)
    }

    fn step(&self, i: usize, d: usize) -> Option<usize> {
        let (q, r) = self.qr(i);
        self.idx(q + DIRS[d].0, r + DIRS[d].1)
    }

    fn cost(&self, i: usize) -> i32 {
        self.terrains[self.tiles[i]].move_cost
    }

    fn naval_cost(&self, i: usize) -> i32 {
        self.terrains[self.tiles[i]].naval
    }

    /// What it costs a unit of type `ty` to enter hex `i`, or 0 if it can't.
    fn enter(&self, ty: usize, i: usize) -> i32 {
        if self.types[ty].naval {
            self.naval_cost(i)
        } else {
            self.cost(i)
        }
    }

    /// Labels the connected areas of the hexes `open` accepts.
    fn areas(&self, open: impl Fn(&Bot, usize) -> bool) -> Vec<u32> {
        let n = self.tiles.len();
        let mut out = vec![NONE; n];
        let mut next = 0;
        for s in 0..n {
            if out[s] != NONE || !open(self, s) {
                continue;
            }
            out[s] = next;
            let mut stack = vec![s];
            while let Some(i) = stack.pop() {
                for d in 0..6 {
                    if let Some(nb) = self.step(i, d) {
                        if out[nb] == NONE && open(self, nb) {
                            out[nb] = next;
                            stack.push(nb);
                        }
                    }
                }
            }
            next += 1;
        }
        out
    }

    fn hexdist(&self, a: usize, b: usize) -> i32 {
        dist(self.qr(a), self.qr(b))
    }

    /// Move cost from every hex to the nearest goal, by terrain alone.
    fn field(&self, goals: &[usize]) -> Vec<i32> {
        let mut d = vec![FAR; self.tiles.len()];
        let mut heap = BinaryHeap::new();
        for &g in goals {
            if self.cost(g) > 0 && d[g] > 0 {
                d[g] = 0;
                heap.push(Reverse((0, g)));
            }
        }
        while let Some(Reverse((c, i))) = heap.pop() {
            if c > d[i] {
                continue;
            }
            let enter = self.cost(i);
            for dir in 0..6 {
                if let Some(n) = self.step(i, dir) {
                    if self.cost(n) > 0 && c + enter < d[n] {
                        d[n] = c + enter;
                        heap.push(Reverse((c + enter, n)));
                    }
                }
            }
        }
        d
    }

    /// Recounts who stands where, and the enemy's zones of control. Units
    /// aboard a transport take no room and exert no zone of control.
    fn mark(&mut self) {
        let n = self.tiles.len();
        self.friends = vec![0; n];
        self.enemy = vec![false; n];
        self.zoc = vec![false; n];
        for u in self.units.iter().filter(|u| u.alive && u.carrier == 0) {
            if u.owner == self.me {
                self.friends[u.at] += 1;
            } else {
                self.enemy[u.at] = true;
                if self.types[u.ty].zoc {
                    for d in 0..6 {
                        if let Some(nb) = self.step(u.at, d) {
                            self.zoc[nb] = true;
                        }
                    }
                }
            }
        }
    }

    /// Every hex unit `u` can get to this turn, on land or, for a ship, on
    /// water, with the move cost and path there. It passes through hexes
    /// full of its own units, but can't stop in them.
    fn search(&self, u: usize) -> HashMap<usize, (i32, String)> {
        let unit = &self.units[u];
        let mut best: HashMap<usize, (i32, String)> = HashMap::new();
        best.insert(unit.at, (0, String::new()));
        if unit.mp <= 0 || unit.acted || unit.fresh || unit.carrier != 0 {
            return best;
        }
        let skirmish = self.types[unit.ty].skirmish;
        let mut heap = BinaryHeap::new();
        heap.push(Reverse((0, unit.at)));
        while let Some(Reverse((c, i))) = heap.pop() {
            if c > best[&i].0 {
                continue;
            }
            if i != unit.at && !skirmish && self.zoc[i] {
                continue;
            }
            let path = best[&i].1.clone();
            for d in 0..6 {
                let Some(n) = self.step(i, d) else { continue };
                let enter = self.enter(unit.ty, n);
                if enter == 0 || self.enemy[n] || c + enter > unit.mp {
                    continue;
                }
                if best.get(&n).is_none_or(|b| c + enter < b.0) {
                    best.insert(n, (c + enter, format!("{path}{d}")));
                    heap.push(Reverse((c + enter, n)));
                }
            }
        }
        best
    }

    /// Where unit `u` can end a move this turn, with the path there.
    fn reach(&self, u: usize) -> Vec<(usize, String)> {
        let at = self.units[u].at;
        let limit = self.rule("stack_limit").max(1) as u8;
        let mut out: Vec<(i32, usize, String)> = self
            .search(u)
            .into_iter()
            .filter(|(h, _)| *h == at || self.friends[*h] < limit)
            .map(|(h, (c, path))| (c, h, path))
            .collect();
        // Nearest first, as the search reaches them.
        out.sort();
        out.into_iter().map(|(_, h, path)| (h, path)).collect()
    }

    /// The path by which land unit `u` boards transport `t` this turn, if it
    /// can: a move whose last step enters the transport's hex.
    fn board_path(&self, u: usize, t: usize) -> Option<String> {
        let (t_at, t_id) = (self.units[t].at, self.units[t].id);
        if self.passengers(t_id).len() as i64 >= self.pmax || !self.units[t].alive {
            return None;
        }
        let (at, mp) = (self.units[u].at, self.units[u].mp);
        let skirmish = self.types[self.units[u].ty].skirmish;
        let mut best: Option<(i32, String)> = None;
        for (h, (c, path)) in self.search(u) {
            if c + 1 > mp || (h != at && !skirmish && self.zoc[h]) {
                continue;
            }
            if let Some(d) = (0..6).find(|&d| self.step(h, d) == Some(t_at)) {
                let p = format!("{path}{d}");
                if best.as_ref().is_none_or(|b| (c, &p) < (b.0, &b.1)) {
                    best = Some((c, p));
                }
            }
        }
        best.map(|b| b.1)
    }

    /// The units aboard transport `id`.
    fn passengers(&self, id: u32) -> Vec<usize> {
        (0..self.units.len())
            .filter(|&p| self.units[p].alive && self.units[p].carrier == id)
            .collect()
    }

    // Orders, keeping the state current.

    fn go(&mut self, u: usize, path: &str) {
        if path.is_empty() || !self.time_left() {
            return;
        }
        let id = self.units[u].id;
        let t = self.order(&format!("M {id} {path}"));
        if t[0] == "OK" || t[0] == "PART" {
            let (q, r) = (t[1].parse().unwrap_or(0), t[2].parse().unwrap_or(0));
            let at = self.idx(q, r).unwrap_or(self.units[u].at);
            if at != self.units[u].at {
                self.units[u].fort = 0;
                self.units[u].moved = true;
            }
            let was_aboard = self.units[u].carrier != 0;
            self.units[u].at = at;
            self.units[u].mp = if t[0] == "OK" { t[3].parse().unwrap_or(0) } else { 0 };
            if self.types[self.units[u].ty].naval {
                // Its passengers ride along.
                for p in 0..self.units.len() {
                    if self.units[p].alive && self.units[p].carrier == id {
                        self.units[p].at = at;
                    }
                }
            } else if self.cost(at) > 0 {
                self.units[u].carrier = 0; // on land: it landed, if it was aboard
            } else if !was_aboard {
                // It boarded the transport there.
                let me = self.me;
                let carrier = (0..self.units.len())
                    .find(|&x| {
                        let v = &self.units[x];
                        v.alive && v.owner == me && v.at == at && v.carrier == 0 && self.types[v.ty].transport
                    })
                    .map(|x| self.units[x].id);
                self.units[u].carrier = carrier.unwrap_or(0);
                self.units[u].mp = 0;
            }
        }
        self.mark();
    }

    fn attack(&mut self, u: usize, e: usize) {
        if !self.time_left() {
            return;
        }
        let t = self.order(&format!("A {} {}", self.units[u].id, self.units[e].id));
        if t[0] == "OK" {
            self.units[e].hp = t[1].parse().unwrap_or(0);
            self.units[u].hp = t[2].parse().unwrap_or(0);
            for v in [u, e] {
                if self.units[v].hp <= 0 {
                    self.units[v].alive = false;
                }
            }
        }
        self.units[u].acted = true;
        self.units[u].mp = 0;
        self.mark();
    }

    fn capture(&mut self, u: usize) {
        if !self.time_left() {
            return;
        }
        let at = self.units[u].at;
        let t = self.order(&format!("C {}", self.units[u].id));
        self.units[u].acted = true;
        if t[0] == "OK" && t[1] == "captured" {
            let me = self.me;
            for p in self
                .cities
                .iter_mut()
                .chain(self.sites.iter_mut())
                .filter(|p| p.at == at)
            {
                p.owner = me;
                p.built = true;
            }
        }
    }

    fn carry(&mut self, op: char, u: usize, amount: Option<i64>) {
        if !self.time_left() {
            return;
        }
        let extra = amount.map_or(String::new(), |a| format!(" {}", a.max(0)));
        let t = self.order(&format!("{op} {}{extra}", self.units[u].id));
        if t[0] == "OK" {
            let num = |i: usize| t.get(i).and_then(|s| s.parse::<i64>().ok()).unwrap_or(0);
            let at = self.units[u].at;
            self.units[u].wheat = num(1);
            self.units[u].bread = num(2);
            self.units[u].mp = 0;
            for p in self
                .cities
                .iter_mut()
                .chain(self.sites.iter_mut())
                .filter(|p| p.at == at)
            {
                p.stock = num(3);
            }
        }
    }

    // Judging fights.

    fn dmg(&self, att: usize, att_hp: i32, def: usize, def_at: usize, def_fort: i32) -> i32 {
        let base = self.damage[att][def];
        if base == 0 {
            return 0;
        }
        let defense = (self.terrains[self.tiles[def_at]].defense + def_fort * self.rule("fortify_pct") as i32).min(100);
        (base * att_hp * (100 - defense) / (self.types[att].max_hp * 100)).max(1)
    }

    fn value(&self, u: &Unit, hp: i32) -> f64 {
        let t = &self.types[u.ty];
        t.cost as f64 * hp as f64 / t.max_hp as f64 + (u.wheat + u.bread) as f64 / 4.0
    }

    /// What u attacking e from where u stands is worth: the value it takes
    /// minus the value it loses to the strike back.
    fn trade(&self, u: usize, e: usize) -> Option<f64> {
        self.trade_at(u, self.units[u].at, e)
    }

    /// The same, for u attacking from hex `from`. Sinking a transport sinks
    /// everything aboard, and units aboard can't be attacked.
    fn trade_at(&self, u: usize, from: usize, e: usize) -> Option<f64> {
        let (a, d) = (&self.units[u], &self.units[e]);
        let range = self.types[a.ty].range;
        let gap = self.hexdist(from, d.at);
        if d.carrier != 0
            || gap < range.0
            || gap > range.1
            || self.damage[a.ty][d.ty] == 0
            || (self.types[a.ty].no_move_attack && (a.moved || from != a.at))
        {
            return None;
        }
        let hit = self.dmg(a.ty, a.hp, d.ty, d.at, d.fort).min(d.hp);
        let mut gain = self.value(d, hit);
        let left = d.hp - hit;
        if left <= 0 {
            gain += self.types[d.ty].cost as f64 * 0.5;
            gain += self
                .units
                .iter()
                .filter(|p| p.alive && p.carrier == d.id)
                .map(|p| self.value(p, p.hp))
                .sum::<f64>();
        }
        let back_range = self.types[d.ty].range;
        let mut loss = 0.0;
        if left > 0 && gap >= back_range.0 && gap <= back_range.1 {
            let fort = if from == a.at { a.fort } else { 0 };
            let back = self.dmg(d.ty, left, a.ty, from, fort).min(a.hp);
            loss = self.value(a, back);
            if back >= a.hp {
                loss += self.types[a.ty].cost as f64 * 0.5;
            }
        }
        Some(gain - loss)
    }

    /// Whether u is ours and can still act. Units aboard a transport only
    /// land, which the transport's plan sees to.
    fn ready(&self, u: usize) -> bool {
        let x = &self.units[u];
        x.alive && x.owner == self.me && !x.acted && !x.fresh && x.carrier == 0
    }

    /// Every unit that can makes its best worthwhile attack from where it stands.
    fn attack_all(&mut self) {
        for u in 0..self.units.len() {
            if !self.ready(u) || !self.time_left() {
                continue;
            }
            let best = (0..self.units.len())
                .filter(|&e| self.units[e].alive && self.units[e].owner != self.me)
                .filter_map(|e| self.trade(u, e).map(|s| (s, e)))
                .max_by(|a, b| a.0.total_cmp(&b.0));
            if let Some((score, e)) = best {
                if score > 0.0 {
                    self.attack(u, e);
                }
            }
        }
    }

    fn capture_all(&mut self) {
        for u in 0..self.units.len() {
            if !self.ready(u) || !self.types[self.units[u].ty].capture {
                continue;
            }
            let at = self.units[u].at;
            let me = self.me;
            if self
                .cities
                .iter()
                .chain(self.sites.iter())
                .any(|p| p.at == at && p.owner != me)
            {
                self.capture(u);
            }
        }
    }

    /// Moves u to the hex it can reach that is nearest the field's goals.
    /// With `wary`, it stays a hex away from visible enemies.
    fn walk(&mut self, u: usize, field: &[i32], wary: bool) {
        let here = field[self.units[u].at];
        let mut best: Option<(i32, usize, String)> = None;
        for (h, path) in self.reach(u) {
            let mut score = field[h];
            if wary && (0..6).any(|d| self.step(h, d).is_some_and(|n| self.enemy[n])) {
                score += 12;
            }
            if best.as_ref().is_none_or(|b| (score, path.len()) < (b.0, b.2.len())) {
                best = Some((score, h, path));
            }
        }
        if let Some((score, _, path)) = best {
            if score < here || (score == here && path.is_empty()) {
                self.go(u, &path);
            }
        }
    }

    // Supply.

    fn feed(&mut self) {
        let enough = 3 * self.rule("ration_cost");
        let me = self.me;
        let mut spots = Vec::new();
        let mut sources: Vec<usize> = self
            .cities
            .iter()
            .filter(|c| c.owner == me && c.stock >= enough)
            .map(|c| c.at)
            .collect();
        sources.extend(
            self.units
                .iter()
                .filter(|u| u.alive && u.owner == me && self.types[u.ty].carry && u.bread >= enough)
                .map(|u| u.at),
        );
        for s in sources {
            spots.push(s);
            spots.extend((0..6).filter_map(|d| self.step(s, d)));
        }
        self.food = self.field(&spots);
        for u in 0..self.units.len() {
            if !self.ready(u) || self.types[self.units[u].ty].carry {
                continue;
            }
            let d = self.food[self.units[u].at];
            if d >= FAR {
                continue;
            }
            let turns = (d + self.types[self.units[u].ty].mv - 1) / self.types[self.units[u].ty].mv.max(1);
            if self.units[u].rations <= 2 * turns + 6 {
                self.units[u].busy = true;
                if d > 0 && self.time_left() {
                    let food = std::mem::take(&mut self.food);
                    self.walk(u, &food, false);
                    self.food = food;
                }
            }
        }
    }

    fn near_own_city(&self, at: usize) -> bool {
        self.cities
            .iter()
            .any(|c| c.owner == self.me && self.hexdist(c.at, at) == 1)
    }

    fn wagons(&mut self) {
        let me = self.me;
        // Sites on a landmass where we hold no city are a ferry's to haul.
        let held = self.held_lands();
        let haul: Vec<usize> = (0..self.sites.len())
            .filter(|&s| {
                self.sites[s].owner == me
                    && !self.enemy[self.sites[s].at]
                    && !self.near_own_city(self.sites[s].at)
                    && held.contains(&self.land[self.sites[s].at])
            })
            .collect();
        let hungry: Vec<usize> = (0..self.units.len())
            .filter(|&u| {
                let x = &self.units[u];
                x.alive
                    && x.owner == me
                    && x.carrier == 0
                    && !self.types[x.ty].carry
                    && !self.types[x.ty].naval
                    && x.rations <= self.rule("ration_max") as i32 / 2
                    && self.food[x.at] > 0
                    && self.food[x.at] < FAR
            })
            .collect();
        let mut wagons: Vec<usize> = (0..self.units.len())
            .filter(|&u| {
                self.ready(u) && self.types[self.units[u].ty].carry && !self.haulers.contains_key(&self.units[u].id)
            })
            .collect();
        // A port saving for a ship gets bread brought to it by one wagon.
        if self.naval {
            wagons.retain(|&w| !self.stock_port(w));
        }
        for (k, &w) in wagons.iter().enumerate() {
            if !self.time_left() {
                return;
            }
            let id = self.units[w].id;
            if k % 3 == 2 && !hungry.is_empty() && self.units[w].wheat == 0 {
                self.jobs.remove(&id);
                self.feeder(w, &hungry);
                continue;
            }
            // Deliver a load to the nearest city.
            let site = self.jobs.get(&id).copied().filter(|s| haul.contains(s));
            let site_wheat = site.map_or(0, |s| self.sites[s].stock);
            if self.units[w].wheat >= LOAD || (self.units[w].wheat > 0 && (site.is_none() || site_wheat == 0)) {
                let Some(c) = self.nearest_city(self.units[w].at, 0) else {
                    continue;
                };
                if self.units[w].at != self.cities[c].at {
                    let f = self.city_field[c].clone();
                    self.walk(w, &f, true);
                }
                if self.units[w].at == self.cities[c].at {
                    self.carry('U', w, None);
                    self.jobs.remove(&id);
                }
                continue;
            }
            // Otherwise fetch wheat from the fullest site nearby that no one else is on.
            let site = site.or_else(|| {
                let taken: Vec<usize> = self.jobs.values().copied().collect();
                haul.iter().copied().filter(|s| !taken.contains(s)).max_by_key(|&s| {
                    self.sites[s].stock + 30 * self.sites[s].output - 40 * self.site_field[s][self.units[w].at] as i64
                })
            });
            let Some(s) = site else { continue };
            self.jobs.insert(id, s);
            if self.units[w].at != self.sites[s].at {
                let f = self.site_field[s].clone();
                self.walk(w, &f, true);
            }
            if self.units[w].at == self.sites[s].at && self.sites[s].stock >= LOAD.min(self.sites[s].cap) {
                self.carry('L', w, None);
            }
        }
    }

    /// If a port on wagon w's landmass is saving for a ship, and no other
    /// wagon is stocking it, w carries bread there from the city on the
    /// landmass with the most to spare. Returns whether w took the job.
    fn stock_port(&mut self, w: usize) -> bool {
        let me = self.me;
        let id = self.units[w].id;
        let l = self.land[self.units[w].at];
        if self.units[w].wheat > 0 {
            return false;
        }
        let mut count: HashMap<char, f64> = HashMap::new();
        for u in self.units.iter().filter(|u| u.alive && u.owner == me) {
            *count.entry(self.types[u.ty].code).or_insert(0.0) += 1.0;
        }
        let needy = (0..self.cities.len()).find(|&c| {
            self.cities[c].owner == me
                && self.land[self.cities[c].at] == l
                && self.cities[c].stock < 400
                && self.ship_wanted(c, i64::MAX, &count).is_some()
        });
        let Some(port) = needy else {
            self.jobs.retain(|&x, &mut s| x != id || s != usize::MAX);
            return false;
        };
        // One stocker per port: the wagon already on it, or the nearest.
        let stocker = self.jobs.iter().find(|(_, s)| **s == usize::MAX).map(|(&x, _)| x);
        if stocker.is_some_and(|x| x != id) {
            return false;
        }
        let spare = |c: &Place| c.stock - KEEP;
        let source = (0..self.cities.len())
            .filter(|&c| c != port && self.cities[c].owner == me && self.land[self.cities[c].at] == l)
            .filter(|&c| spare(&self.cities[c]) >= 100 && !self.enemy[self.cities[c].at])
            .max_by_key(|&c| (spare(&self.cities[c]), Reverse(c)));
        if self.units[w].bread < 100 && source.is_none() {
            return false;
        }
        self.jobs.insert(id, usize::MAX);
        let goal = if self.units[w].bread >= 100 { port } else { source.unwrap_or(port) };
        if self.units[w].at != self.cities[goal].at {
            let f = self.city_field[goal].clone();
            self.walk(w, &f, true);
        }
        if self.units[w].at == self.cities[goal].at {
            if goal == port {
                self.carry('U', w, None);
                self.jobs.remove(&id);
            } else {
                let room = self.rule("cargo_max") - self.units[w].bread;
                let amount = room.min(spare(&self.cities[goal]));
                self.carry('L', w, Some(amount));
            }
        }
        true
    }

    fn feeder(&mut self, w: usize, hungry: &[usize]) {
        if self.units[w].bread < 100 {
            let Some(c) = self.nearest_city(self.units[w].at, 300) else {
                return;
            };
            if self.units[w].at != self.cities[c].at {
                let f = self.city_field[c].clone();
                self.walk(w, &f, true);
            }
            if self.units[w].at == self.cities[c].at {
                let room = self.rule("cargo_max") - self.units[w].wheat - self.units[w].bread;
                let spare = self.cities[c].stock - 2 * KEEP;
                self.carry('L', w, Some(room.min(spare)));
            }
            return;
        }
        let Some(&target) = hungry
            .iter()
            .min_by_key(|&&u| (self.units[u].rations, self.hexdist(self.units[u].at, self.units[w].at)))
        else {
            return;
        };
        let at = self.units[target].at;
        let goal: Vec<usize> = std::iter::once(at)
            .chain((0..6).filter_map(|d| self.step(at, d)))
            .collect();
        let f = self.field(&goal);
        self.walk(w, &f, true);
    }

    /// Our city nearest `at` with `need` bread to spare and no enemy on it.
    fn nearest_city(&self, at: usize, need: i64) -> Option<usize> {
        (0..self.cities.len())
            .filter(|&c| {
                self.cities[c].owner == self.me && !self.enemy[self.cities[c].at] && self.cities[c].stock - KEEP >= need
            })
            .min_by_key(|&c| (self.city_field[c][at], c))
    }

    // The army.

    fn enemy_cities(&self) -> Vec<usize> {
        self.cities
            .iter()
            .filter(|c| c.owner != self.me && c.owner != 0)
            .map(|c| c.at)
            .collect()
    }

    /// Cavalry and scouts go for enemy wagons, then enemy sites and cities
    /// with no one on them, the nearest first.
    fn raid(&mut self) {
        let me = self.me;
        let mut goals: Vec<usize> = self
            .units
            .iter()
            .filter(|u| u.alive && u.owner != me && self.types[u.ty].carry)
            .map(|u| u.at)
            .collect();
        goals.extend(
            self.sites
                .iter()
                .filter(|s| s.owner != me && s.owner != 0 && !self.enemy[s.at])
                .map(|s| s.at),
        );
        if goals.is_empty() {
            goals = self
                .cities
                .iter()
                .filter(|c| c.owner != me && c.owner != 0 && !self.enemy[c.at])
                .map(|c| c.at)
                .collect();
        }
        if goals.is_empty() {
            return;
        }
        let f = self.field(&goals);
        for u in 0..self.units.len() {
            let t = &self.types[self.units[u].ty];
            // Without supply there's nothing to raid but cities, a job for scouts only.
            let raider = !t.capture && !t.carry && !t.naval && t.range.1 <= 1 && (self.supply || t.skirmish);
            if !self.ready(u) || self.units[u].busy || !raider {
                continue;
            }
            if !self.time_left() {
                return;
            }
            if f[self.units[u].at] >= FAR {
                continue; // nothing to raid on this landmass: free to sail
            }
            self.units[u].busy = true;
            if f[self.units[u].at] > 0 {
                self.walk(u, &f, false);
            }
        }
    }

    /// One pikeman holds each of our cities nearest the enemy, and digs in.
    fn hold(&mut self) {
        let theirs = self.enemy_cities();
        if theirs.is_empty() {
            return;
        }
        let front = self.field(&theirs);
        let me = self.me;
        let mut cities: Vec<usize> = (0..self.cities.len())
            .filter(|&c| self.cities[c].owner == me && front[self.cities[c].at] <= 24)
            .collect();
        cities.sort_by_key(|&c| front[self.cities[c].at]);
        for c in cities {
            let at = self.cities[c].at;
            let field = &self.city_field[c];
            let holder = (0..self.units.len())
                .filter(|&u| self.ready(u) && !self.units[u].busy && self.types[self.units[u].ty].code == 'P')
                .filter(|&u| field[self.units[u].at] <= 30)
                .min_by_key(|&u| (field[self.units[u].at], self.units[u].id));
            let Some(u) = holder else { break };
            self.units[u].busy = true;
            if self.units[u].at != at && self.time_left() {
                let f = field.clone();
                self.walk(u, &f, false);
            }
        }
    }

    /// Capturers go for the nearest city or site no other unit is after;
    /// everyone else marches on the nearest enemy city.
    fn spread(&mut self) {
        let me = self.me;
        let mut plans: Vec<(i32, usize, usize, bool)> = Vec::new();
        for u in 0..self.units.len() {
            if !self.ready(u) || self.units[u].busy || !self.types[self.units[u].ty].capture {
                continue;
            }
            let (at, mv) = (self.units[u].at, self.types[self.units[u].ty].mv.max(1));
            for (k, _) in self.cities.iter().enumerate().filter(|(_, p)| p.owner != me) {
                let turns = self.city_field[k][at] / mv;
                if turns <= TARGET_TURNS {
                    plans.push((turns, u, k, true));
                }
            }
            for (k, _) in self.sites.iter().enumerate().filter(|(_, p)| p.owner != me) {
                let turns = self.site_field[k][at] / mv;
                if turns <= TARGET_TURNS {
                    plans.push((turns, u, k, false));
                }
            }
        }
        plans.sort();
        let mut sent: HashMap<(bool, usize), usize> = HashMap::new();
        for (_, u, k, city) in plans {
            if self.units[u].busy || !self.time_left() {
                continue;
            }
            let room = if city { 2 } else { 1 };
            let n = sent.entry((city, k)).or_insert(0);
            if *n >= room {
                continue;
            }
            *n += 1;
            self.units[u].busy = true;
            let f = if city {
                self.city_field[k].clone()
            } else {
                self.site_field[k].clone()
            };
            self.walk(u, &f, false);
        }
        // Everyone else hunts the visible enemy units, or failing that marches
        // on the enemy's cities.
        let me = self.me;
        let mut goals: Vec<usize> = self
            .units
            .iter()
            .filter(|u| u.alive && u.owner != me)
            .map(|u| u.at)
            .collect();
        if goals.is_empty() {
            goals = self.enemy_cities();
        }
        if goals.is_empty() {
            return;
        }
        let f = self.field(&goals);
        for u in 0..self.units.len() {
            let t = &self.types[self.units[u].ty];
            if self.ready(u) && !self.units[u].busy && !t.carry && !t.naval && self.time_left() {
                self.walk(u, &f, false);
            }
        }
    }

    // Ships.

    /// Landmasses where we hold a city.
    fn held_lands(&self) -> Vec<u32> {
        let me = self.me;
        let mut out: Vec<u32> = self
            .cities
            .iter()
            .filter(|c| c.owner == me)
            .map(|c| self.land[c.at])
            .collect();
        out.sort_unstable();
        out.dedup();
        out
    }

    /// Move cost over water from every hex to the nearest goal, for ships.
    /// The sea never changes, so fields are kept.
    fn sea_field(&mut self, goals: &[usize]) -> Vec<i32> {
        let mut key = goals.to_vec();
        key.sort_unstable();
        key.dedup();
        if let Some(f) = self.sea_cache.get(&key) {
            return f.clone();
        }
        let mut d = vec![FAR; self.tiles.len()];
        let mut heap = BinaryHeap::new();
        for &g in &key {
            if self.naval_cost(g) > 0 {
                d[g] = 0;
                heap.push(Reverse((0, g)));
            }
        }
        while let Some(Reverse((c, i))) = heap.pop() {
            if c > d[i] {
                continue;
            }
            let enter = self.naval_cost(i);
            for dir in 0..6 {
                if let Some(n) = self.step(i, dir) {
                    if self.naval_cost(n) > 0 && c + enter < d[n] {
                        d[n] = c + enter;
                        heap.push(Reverse((c + enter, n)));
                    }
                }
            }
        }
        if self.sea_cache.len() > 2000 {
            self.sea_cache.clear();
        }
        self.sea_cache.insert(key, d.clone());
        d
    }

    /// The water from which units land nearest `target`, by the walk from
    /// the shore.
    fn landings(&mut self, target: usize) -> Vec<usize> {
        if let Some(v) = self.landing_cache.get(&target) {
            return v.clone();
        }
        let field = self.field(&[target]);
        let mut spots: Vec<(i32, usize)> = Vec::new();
        for w in 0..self.tiles.len() {
            if self.naval_cost(w) == 0 {
                continue;
            }
            let d = (0..6)
                .filter_map(|dir| self.step(w, dir))
                .filter(|&n| self.cost(n) > 0)
                .map(|n| field[n])
                .min()
                .unwrap_or(FAR);
            if d < FAR {
                spots.push((d, w));
            }
        }
        let low = spots.iter().map(|s| s.0).min().unwrap_or(0);
        let out: Vec<usize> = spots.into_iter().filter(|s| s.0 <= low + 2).map(|s| s.1).collect();
        self.landing_cache.insert(target, out.clone());
        out
    }

    /// The water next to city `c` on sea `sea`, where its ships dock.
    fn port_water(&self, c: usize, sea: u32) -> Vec<usize> {
        let at = self.cities[c].at;
        (0..6)
            .filter_map(|d| self.step(at, d))
            .filter(|&n| self.naval_cost(n) > 0 && self.sea[n] == sea)
            .collect()
    }

    /// Our sites on landmass `isle` with no enemy on them.
    fn isle_sites(&self, isle: u32) -> Vec<usize> {
        (0..self.sites.len())
            .filter(|&k| {
                let s = &self.sites[k];
                s.owner == self.me && self.land[s.at] == isle && !self.enemy[s.at]
            })
            .collect()
    }

    /// Landmasses where we hold sites but no city, whose wheat only a ferry
    /// can bring home.
    fn haul_isles(&self) -> Vec<u32> {
        let held = self.held_lands();
        let mut out: Vec<u32> = self
            .sites
            .iter()
            .filter(|s| s.owner == self.me && !self.enemy[s.at] && !held.contains(&self.land[s.at]))
            .map(|s| self.land[s.at])
            .collect();
        out.sort_unstable();
        out.dedup();
        out
    }

    fn haulers_of(&self, isle: u32) -> Vec<usize> {
        let mut out: Vec<usize> = (0..self.units.len())
            .filter(|&w| self.units[w].alive && self.haulers.get(&self.units[w].id) == Some(&isle))
            .collect();
        out.sort_by_key(|&w| self.units[w].id);
        out
    }

    /// Visible enemy land units' value within 3 hexes of `at`.
    fn danger(&self, at: usize) -> f64 {
        self.units
            .iter()
            .filter(|e| e.alive && e.owner != self.me && e.carrier == 0 && !self.types[e.ty].naval)
            .filter(|e| self.hexdist(e.at, at) <= 3)
            .map(|e| self.value(e, e.hp))
            .sum()
    }

    /// The best city or site across the water from water hex `at`, for
    /// units of the given strength from landmass `home`, with the
    /// landmass it's on. Cities we don't hold on landmasses where we hold
    /// none; sites we don't hold on landmasses where no one holds a city,
    /// and our own there once enemy units land. Nearer and richer first, and
    /// ones another transport sails for count half.
    fn best_target(&mut self, at: usize, home: u32, strength: f64, skip: u32) -> Option<((bool, usize), u32)> {
        let me = self.me;
        let sea = self.sea[at];
        let held = self.held_lands();
        let theirs: Vec<u32> = self
            .cities
            .iter()
            .filter(|c| c.owner != me && c.owner != 0)
            .map(|c| self.land[c.at])
            .collect();
        let invaded: Vec<u32> = self
            .units
            .iter()
            .filter(|e| e.alive && e.owner != me && e.carrier == 0 && !self.types[e.ty].naval)
            .map(|e| self.land[e.at])
            .collect();
        let others: Vec<(bool, usize)> = self
            .ships
            .iter()
            .filter(|(id, s)| **id != skip && s.phase == Phase::Out)
            .filter_map(|(_, s)| s.target)
            .collect();
        let mut cands: Vec<((bool, usize), f64)> = Vec::new();
        for (k, c) in self.cities.iter().enumerate() {
            let l = self.land[c.at];
            if c.owner != me && l != home && !held.contains(&l) {
                cands.push(((true, k), if c.owner == 0 { 1.0 } else { 0.5 }));
            }
        }
        for (k, s) in self.sites.iter().enumerate() {
            let l = self.land[s.at];
            if l != home && !held.contains(&l) && !theirs.contains(&l) && (s.owner != me || invaded.contains(&l)) {
                cands.push(((false, k), s.output.max(1) as f64 / 100.0));
            }
        }
        let mv = self.transport.map_or(1, |t| self.types[t].mv.max(1));
        let mut best: Option<(f64, (bool, usize), u32)> = None;
        for (key, value) in cands {
            let (hex, owner) = if key.0 {
                (self.cities[key.1].at, self.cities[key.1].owner)
            } else {
                (self.sites[key.1].at, self.sites[key.1].owner)
            };
            if owner != me && self.danger(hex) > 1.5 * strength + 100.0 {
                continue;
            }
            let spots: Vec<usize> = self.landings(hex).into_iter().filter(|&w| self.sea[w] == sea).collect();
            if spots.is_empty() {
                continue;
            }
            let d = self.sea_field(&spots)[at];
            if d >= FAR {
                continue;
            }
            let mut score = value / (d as f64 / mv as f64 + 2.0);
            if others.contains(&key) {
                score *= 0.5;
            }
            if best.as_ref().is_none_or(|b| score > b.0) {
                best = Some((score, key, self.land[hex]));
            }
        }
        best.map(|b| (b.1, b.2))
    }

    fn sea(&mut self) {
        if !self.naval {
            return;
        }
        let me = self.me;
        let round = self.round;
        let seen: Vec<u32> = self
            .units
            .iter()
            .filter(|u| u.alive && u.owner != me && self.types[u.ty].naval)
            .map(|u| u.id)
            .collect();
        for id in seen {
            self.seen_ships.insert(id, round);
        }
        let alive: Vec<u32> = self.units.iter().filter(|u| u.alive).map(|u| u.id).collect();
        self.ships.retain(|id, _| alive.contains(id));
        self.crew.retain(|u, t| alive.contains(u) && alive.contains(t));
        self.haulers.retain(|w, _| alive.contains(w));
        self.galleys();
        self.transports();
        self.guard();
        self.plan_ships();
        // A line in the replay on the navy.
        let mut fleet: Vec<String> = Vec::new();
        for u in self.units.iter().filter(|u| u.alive && u.owner == me && self.types[u.ty].naval) {
            let plan = self.ships.get(&u.id).map_or(String::from("galley"), |s| {
                let phase = match s.phase {
                    Phase::Dock => "dock",
                    Phase::Out => "out",
                    Phase::Wait => "wait",
                    Phase::Back => "back",
                };
                format!("{}{}", if s.ferry { "ferry " } else { "" }, phase)
            });
            let aboard = self.units.iter().filter(|p| p.alive && p.carrier == u.id).count();
            fleet.push(format!("{}{} {} {}", self.types[u.ty].code, u.id, plan, aboard));
        }
        let line = format!(
            "D TEXT navy: want {} transports, {} galleys; {} crew, {} haulers; {}",
            self.want_t,
            self.want_g,
            self.crew.len(),
            self.haulers.len(),
            fleet.join(", ")
        );
        self.send(&line);
    }

    /// Galleys strike the best target they can reach, a transport above all,
    /// and otherwise hunt enemy transports or lie off enemy ports. They go
    /// home to eat and heal when they must.
    fn galleys(&mut self) {
        let Some(gt) = self.galley else { return };
        let ids: Vec<usize> = (0..self.units.len())
            .filter(|&u| self.ready(u) && self.units[u].ty == gt)
            .collect();
        for g in ids {
            if !self.time_left() {
                return;
            }
            if !self.units[g].alive || self.units[g].acted {
                continue;
            }
            let hungry = self.units[g].rations < SHIP_RATIONS || 2 * self.units[g].hp < self.types[gt].max_hp;
            if self.strike(g, !hungry) {
                continue;
            }
            let me = self.me;
            let sea = self.sea[self.units[g].at];
            let mut goals: Vec<usize> = Vec::new();
            if hungry {
                for c in 0..self.cities.len() {
                    if self.cities[c].owner == me && self.cities[c].stock >= KEEP {
                        goals.extend(self.port_water(c, sea));
                    }
                }
            } else {
                for e in self.units.iter().filter(|e| e.alive && e.owner != me && self.types[e.ty].transport) {
                    goals.extend(
                        (0..6)
                            .filter_map(|d| self.step(e.at, d))
                            .filter(|&n| self.naval_cost(n) > 0 && self.sea[n] == sea),
                    );
                }
                if goals.is_empty() {
                    for c in 0..self.cities.len() {
                        if self.cities[c].owner != me && self.cities[c].owner != 0 {
                            goals.extend(self.port_water(c, sea));
                        }
                    }
                }
            }
            if goals.is_empty() || goals.contains(&self.units[g].at) {
                continue;
            }
            let f = self.sea_field(&goals);
            self.walk(g, &f, hungry);
        }
    }

    /// Galley g's best worthwhile attack, after a move if `moving`. Returns
    /// whether it made one.
    fn strike(&mut self, g: usize, moving: bool) -> bool {
        let options = if moving {
            self.reach(g)
        } else {
            vec![(self.units[g].at, String::new())]
        };
        let me = self.me;
        let mut best: Option<(f64, usize, String)> = None;
        for e in 0..self.units.len() {
            if !self.units[e].alive || self.units[e].owner == me {
                continue;
            }
            for (h, path) in &options {
                if let Some(score) = self.trade_at(g, *h, e) {
                    if score > 0.0 && best.as_ref().is_none_or(|b| score > b.0) {
                        best = Some((score, e, path.clone()));
                    }
                }
            }
        }
        let Some((_, e, path)) = best else { return false };
        self.go(g, &path);
        if self.units[g].alive && self.units[e].alive && self.trade(g, e).is_some() {
            self.attack(g, e);
        }
        true
    }

    fn transports(&mut self) {
        let me = self.me;
        let Some(tt) = self.transport else { return };
        let mut ids: Vec<usize> = (0..self.units.len())
            .filter(|&u| self.units[u].alive && self.units[u].owner == me && self.units[u].ty == tt)
            .collect();
        ids.sort_by_key(|&t| self.units[t].id);
        for &t in &ids {
            self.ships.entry(self.units[t].id).or_insert(Ship {
                phase: Phase::Dock,
                port: None,
                target: None,
                isle: NONE,
                ferry: false,
                waited: 0,
            });
        }
        // Each landmass only a ferry can haul from gets one: a docked
        // transport with no one aboard or coming.
        let isles = self.haul_isles();
        for &isle in &isles {
            if self.ships.values().any(|s| s.ferry && s.isle == isle) {
                continue;
            }
            let Some(&k) = self.isle_sites(isle).first() else { continue };
            let spots = self.landings(self.sites[k].at);
            let pick = ids.iter().copied().find(|&t| {
                let id = self.units[t].id;
                let s = &self.ships[&id];
                s.phase == Phase::Dock
                    && !s.ferry
                    && s.port.is_some()
                    && self.passengers(id).is_empty()
                    && !self.crew.values().any(|&x| x == id)
                    && spots.iter().any(|&w| self.sea[w] == self.sea[self.units[t].at])
            });
            if let Some(t) = pick {
                if let Some(s) = self.ships.get_mut(&self.units[t].id) {
                    (s.ferry, s.isle, s.waited) = (true, isle, 0);
                }
            }
        }
        // Each ferry gets two wagons from its port's landmass.
        let mut ferries: Vec<(u32, usize)> = self
            .ships
            .values()
            .filter(|s| s.ferry && s.port.is_some())
            .map(|s| (s.isle, s.port.unwrap_or(0)))
            .collect();
        ferries.sort_unstable();
        for (isle, port) in ferries {
            let want = 2usize.min(self.pmax.max(1) as usize);
            let have = self.haulers.values().filter(|&&i| i == isle).count();
            if have >= want {
                continue;
            }
            let home = self.land[self.cities[port].at];
            let mut free: Vec<usize> = (0..self.units.len())
                .filter(|&w| {
                    let x = &self.units[w];
                    x.alive
                        && x.owner == me
                        && self.types[x.ty].carry
                        && x.carrier == 0
                        && !x.fresh
                        && x.wheat == 0
                        && !self.haulers.contains_key(&x.id)
                        && self.land[x.at] == home
                })
                .collect();
            free.sort_by_key(|&w| (self.city_field[port][self.units[w].at], self.units[w].id));
            for w in free.into_iter().take(want - have) {
                let id = self.units[w].id;
                self.haulers.insert(id, isle);
                self.jobs.remove(&id);
            }
        }
        self.assign_crews(&ids);
        let mut claims: HashMap<usize, i64> = HashMap::new();
        for t in ids {
            if !self.time_left() {
                return;
            }
            if self.units[t].alive && !self.units[t].fresh {
                self.run_transport(t, &isles, &mut claims);
            }
        }
    }

    /// Land units that have nothing to do where they are sign on to the
    /// transports docked at their landmass's ports, leaving a garrison on a
    /// small home island.
    fn assign_crews(&mut self, ids: &[usize]) {
        let me = self.me;
        let mut spare: HashMap<u32, i64> = HashMap::new();
        for u in self.units.iter() {
            if u.alive && u.owner == me && u.carrier == 0 && !self.types[u.ty].carry && !self.types[u.ty].naval {
                let coming = self.crew.contains_key(&u.id);
                *spare.entry(self.land[u.at]).or_insert(0) += if coming { 0 } else { 1 };
            }
        }
        for c in self.cities.iter().filter(|c| c.owner == me) {
            let l = self.land[c.at];
            if self.home_isles.contains(&l) {
                *spare.entry(l).or_insert(0) -= GARRISON;
            }
        }
        for &t in ids {
            let id = self.units[t].id;
            let s = self.ships[&id].clone();
            let Some(port) = s.port else { continue };
            if s.phase != Phase::Dock || s.ferry {
                continue;
            }
            let home = self.land[self.cities[port].at];
            let coming = self.crew.values().filter(|&&x| x == id).count() as i64;
            let mut want = self.pmax - self.passengers(id).len() as i64 - coming;
            if want <= 0 || *spare.get(&home).unwrap_or(&0) <= 0 {
                continue;
            }
            let water = self.port_water(port, self.sea[self.units[t].at]);
            let Some(&dock) = water.first() else { continue };
            if self.best_target(dock, home, 1e9, id).is_none() {
                continue; // nowhere worth sailing
            }
            let mut free: Vec<usize> = (0..self.units.len())
                .filter(|&u| {
                    let x = &self.units[u];
                    self.ready(u)
                        && !x.busy
                        && x.mp > 0
                        && !self.types[x.ty].carry
                        && !self.types[x.ty].naval
                        && self.land[x.at] == home
                        && !self.crew.contains_key(&x.id)
                        && self.city_field[port][x.at] < FAR
                })
                .collect();
            free.sort_by_key(|&u| {
                let ty = &self.types[self.units[u].ty];
                (!ty.capture, ty.code == 'P', self.city_field[port][self.units[u].at], self.units[u].id)
            });
            for u in free {
                if want <= 0 || *spare.get(&home).unwrap_or(&0) <= 0 {
                    break;
                }
                *spare.entry(home).or_insert(0) -= 1;
                self.crew.insert(self.units[u].id, id);
                want -= 1;
            }
        }
    }

    /// A transport's turn. A troop transport docks, takes its crew aboard,
    /// sails for the best target, puts everyone ashore, and sails home. A
    /// ferry docks while its wagons unload wheat, take bread, and board;
    /// sails to its landmass and puts them ashore; waits while they load
    /// wheat and come back aboard; and sails home, where they land in the
    /// port.
    fn run_transport(&mut self, t: usize, isles: &[u32], claims: &mut HashMap<usize, i64>) {
        let id = self.units[t].id;
        let Some(mut s) = self.ships.get(&id).cloned() else { return };
        let lost = s.ferry && !isles.contains(&s.isle);
        if s.phase == Phase::Dock {
            let there = self.dock(t, &mut s);
            if let Some(port) = s.port {
                if s.ferry && lost {
                    // Nothing left to haul: everyone ashore, and back to troops.
                    if there {
                        for p in self.passengers(id) {
                            self.hauler_ashore(p, port, s.isle);
                        }
                    }
                    if self.passengers(id).is_empty() {
                        let isle = s.isle;
                        let gone: Vec<u32> = self
                            .haulers
                            .iter()
                            .filter(|(_, i)| **i == isle)
                            .map(|(&w, _)| w)
                            .filter(|&w| self.units.iter().any(|x| x.id == w && self.land[x.at] != isle))
                            .collect();
                        for w in gone {
                            self.haulers.remove(&w);
                        }
                        (s.ferry, s.isle) = (false, NONE);
                    }
                } else if s.ferry {
                    self.ferry_board(t, &s, there);
                    let home = self.land[self.cities[port].at];
                    let mine = self.haulers_of(s.isle);
                    let aboard = mine.iter().filter(|&&w| self.units[w].carrier == id).count();
                    let behind = mine
                        .iter()
                        .filter(|&&w| self.units[w].carrier == 0 && self.land[self.units[w].at] == home)
                        .count();
                    if there && aboard > 0 && (behind == 0 || s.waited >= SAIL_WAIT) {
                        (s.phase, s.waited) = (Phase::Out, 0);
                    } else if aboard > 0 {
                        s.waited += 1;
                    }
                } else {
                    self.crew_board(t, port, there);
                    let aboard = self.passengers(id);
                    let coming = self.crew.values().filter(|&&x| x == id).count();
                    let n = aboard.len() as i64;
                    if there && n > 0 && (n >= self.pmax || coming == 0 || s.waited >= SAIL_WAIT) {
                        let strength: f64 = aboard.iter().map(|&p| self.value(&self.units[p], self.units[p].hp)).sum();
                        let home = self.land[self.cities[port].at];
                        let at = self.units[t].at;
                        if let Some((key, isle)) = self.best_target(at, home, strength, id) {
                            (s.phase, s.target, s.isle, s.waited) = (Phase::Out, Some(key), isle, 0);
                            self.crew.retain(|_, x| *x != id);
                        } else {
                            s.waited += 1;
                            if s.waited > 4 * SAIL_WAIT {
                                let f = self.city_field[port].clone();
                                for p in aboard {
                                    self.land_toward(p, &f);
                                }
                                self.crew.retain(|_, x| *x != id);
                                s.waited = 0;
                            }
                        }
                    } else if n > 0 {
                        s.waited += 1;
                    }
                }
            }
        }
        if s.phase == Phase::Out && self.units[t].alive {
            let goal = if s.ferry {
                self.isle_sites(s.isle).first().map(|&k| (self.sites[k].at, None))
            } else {
                s.target.map(|(city, k)| {
                    if city {
                        (self.cities[k].at, Some(self.city_field[k].clone()))
                    } else {
                        (self.sites[k].at, Some(self.site_field[k].clone()))
                    }
                })
            };
            match goal {
                Some((goal, field)) if !lost => {
                    let sea = self.sea[self.units[t].at];
                    let spots: Vec<usize> = self.landings(goal).into_iter().filter(|&w| self.sea[w] == sea).collect();
                    if !spots.is_empty() && !spots.contains(&self.units[t].at) && self.units[t].mp > 0 {
                        let f = self.sea_field(&spots);
                        self.walk(t, &f, true);
                    }
                    if self.units[t].alive && spots.contains(&self.units[t].at) {
                        for p in self.passengers(id) {
                            match &field {
                                None => self.hauler_land(p, s.isle, claims),
                                Some(f) => {
                                    self.land_toward(p, f);
                                    self.capture_here(p);
                                }
                            }
                        }
                        s.phase = if s.ferry { Phase::Wait } else { Phase::Back };
                        s.waited = 0;
                    }
                }
                _ => s.phase = Phase::Back,
            }
        }
        if s.phase == Phase::Wait && self.units[t].alive {
            s.waited += 1;
            let isle = s.isle;
            for w in self.haulers_of(isle) {
                if self.units[w].carrier == 0 && self.land[self.units[w].at] == isle {
                    self.hauler_island(w, t, &s, lost, claims);
                }
            }
            let ashore = self
                .haulers_of(isle)
                .into_iter()
                .filter(|&w| self.units[w].carrier == 0 && self.land[self.units[w].at] == isle)
                .count();
            if ashore == 0 || s.waited > 2 * FERRY_WAIT || (lost && s.waited > 2) {
                s.phase = Phase::Back;
            }
        }
        if s.phase == Phase::Back && self.units[t].alive && self.dock(t, &mut s) {
            if let Some(port) = s.port {
                for p in self.passengers(id) {
                    if self.haulers.contains_key(&self.units[p].id) {
                        self.hauler_ashore(p, port, s.isle);
                    } else {
                        let f = self.city_field[port].clone();
                        self.land_toward(p, &f);
                    }
                }
            }
            if self.passengers(id).is_empty() {
                (s.phase, s.target, s.waited) = (Phase::Dock, None, 0);
            }
        }
        self.ships.insert(id, s);
    }

    /// Sails transport t toward the water by its port, picking the nearest
    /// of our ports on its sea if it has none. Returns whether it's there.
    fn dock(&mut self, t: usize, s: &mut Ship) -> bool {
        let me = self.me;
        let at = self.units[t].at;
        let sea = self.sea[at];
        let ports: Vec<usize> = (0..self.cities.len())
            .filter(|&c| self.cities[c].owner == me && !self.port_water(c, sea).is_empty())
            .collect();
        if ports.is_empty() {
            return false;
        }
        if s.port.is_none_or(|p| !ports.contains(&p)) {
            let mut best: Option<(i32, usize)> = None;
            for &c in &ports {
                let water = self.port_water(c, sea);
                let d = self.sea_field(&water)[at];
                if best.is_none_or(|b| (d, c) < b) {
                    best = Some((d, c));
                }
            }
            s.port = best.map(|b| b.1);
        }
        let Some(port) = s.port else { return false };
        let water = self.port_water(port, sea);
        if water.contains(&at) {
            return true;
        }
        if self.units[t].mp > 0 {
            let f = self.sea_field(&water);
            self.walk(t, &f, true);
        }
        water.contains(&self.units[t].at)
    }

    /// Unit p, aboard, lands on the hex next to its transport nearest the
    /// goals of `field`.
    fn land_toward(&mut self, p: usize, field: &[i32]) {
        let x = &self.units[p];
        if x.carrier == 0 || x.fresh || x.mp <= 0 || !self.time_left() {
            return;
        }
        let at = x.at;
        let limit = self.rule("stack_limit").max(1) as u8;
        let best = (0..6)
            .filter_map(|d| self.step(at, d).map(|n| (d, n)))
            .filter(|&(_, n)| self.cost(n) > 0 && !self.enemy[n] && self.friends[n] < limit)
            .min_by_key(|&(d, n)| (field[n], d));
        if let Some((d, _)) = best {
            self.go(p, &d.to_string());
        }
    }

    /// Captures the city or site p stands on, if it can.
    fn capture_here(&mut self, p: usize) {
        if !self.ready(p) || !self.types[self.units[p].ty].capture {
            return;
        }
        let (at, me) = (self.units[p].at, self.me);
        if self.cities.iter().chain(self.sites.iter()).any(|q| q.at == at && q.owner != me) {
            self.capture(p);
        }
    }

    /// The crew of transport t walk to its port and board once it's there.
    fn crew_board(&mut self, t: usize, port: usize, there: bool) {
        let id = self.units[t].id;
        let mut crew: Vec<u32> = self.crew.iter().filter(|(_, x)| **x == id).map(|(&u, _)| u).collect();
        crew.sort_unstable();
        for uid in crew {
            let Some(u) = (0..self.units.len()).find(|&x| self.units[x].id == uid && self.units[x].alive) else {
                continue;
            };
            if !self.ready(u) || self.units[u].mp <= 0 || !self.time_left() {
                continue;
            }
            self.units[u].busy = true;
            if there {
                if let Some(path) = self.board_path(u, t) {
                    self.go(u, &path);
                    if self.units[u].carrier == id {
                        self.crew.remove(&uid);
                    }
                    continue;
                }
            }
            if self.units[u].at != self.cities[port].at {
                let f = self.city_field[port].clone();
                self.walk(u, &f, false);
            }
        }
    }

    /// Bread a ferry's wagon takes out: the first of each landmass's wagons
    /// takes enough for the guards there, and the others none.
    fn guard_need(&self, w: usize, isle: u32) -> i64 {
        if self.haulers_of(isle).first() != Some(&w) {
            return 0;
        }
        let me = self.me;
        let guards = self
            .units
            .iter()
            .filter(|x| {
                x.alive
                    && x.owner == me
                    && x.carrier == 0
                    && self.land[x.at] == isle
                    && !self.types[x.ty].carry
                    && !self.types[x.ty].naval
            })
            .count() as i64;
        (GUARD_BREAD * guards).min(self.rule("cargo_max") / 2)
    }

    /// Unloads wagon w's wheat in the city it stands on, then takes the
    /// bread it should carry out, if the city can spare enough.
    fn restock(&mut self, w: usize, c: usize, isle: u32) {
        if self.units[w].wheat > 0 {
            let wheat = self.units[w].wheat;
            self.carry('U', w, Some(wheat));
        }
        let need = self.guard_need(w, isle) - self.units[w].bread;
        let room = self.rule("cargo_max") - self.units[w].wheat - self.units[w].bread;
        let amount = need.min(self.cities[c].stock - KEEP).min(room);
        if amount >= 50 {
            self.carry('L', w, Some(amount));
        }
    }

    /// A ferry's wagons on its port's landmass unload any wheat in the port,
    /// take bread for the guards, and board once the ferry is in port.
    fn ferry_board(&mut self, t: usize, s: &Ship, there: bool) {
        let Some(port) = s.port else { return };
        let (home, port_at) = (self.land[self.cities[port].at], self.cities[port].at);
        for w in self.haulers_of(s.isle) {
            let x = &self.units[w];
            if x.carrier != 0 || x.fresh || x.mp <= 0 || self.land[x.at] != home || !self.time_left() {
                continue;
            }
            let need = self.guard_need(w, s.isle) - self.units[w].bread;
            if self.units[w].wheat > 0 || (need >= 50 && self.cities[port].stock - KEEP >= 50) {
                if self.units[w].at != port_at {
                    let f = self.city_field[port].clone();
                    self.walk(w, &f, true);
                }
                if self.units[w].at == port_at {
                    self.restock(w, port, s.isle);
                }
                continue;
            }
            if there {
                if let Some(path) = self.board_path(w, t) {
                    self.go(w, &path);
                    continue;
                }
            }
            if self.units[w].at != port_at {
                let f = self.city_field[port].clone();
                self.walk(w, &f, true);
            }
        }
    }

    /// The site on landmass `isle` with the most wheat no other wagon has
    /// claimed this turn, for wagon w, which claims what it has room for.
    fn richest(&self, isle: u32, w: usize, claims: &mut HashMap<usize, i64>) -> Option<usize> {
        let room = self.rule("cargo_max") - self.units[w].wheat - self.units[w].bread;
        let at = self.units[w].at;
        let k = self.isle_sites(isle).into_iter().max_by_key(|&k| {
            (
                self.sites[k].stock - claims.get(&k).copied().unwrap_or(0),
                -self.hexdist(at, self.sites[k].at),
                Reverse(k),
            )
        })?;
        *claims.entry(k).or_insert(0) += room;
        Some(k)
    }

    fn load_site(&mut self, w: usize, k: usize) {
        let room = self.rule("cargo_max") - self.units[w].wheat - self.units[w].bread;
        if room > 0 && self.sites[k].stock > 0 && self.sites[k].owner == self.me {
            self.carry('L', w, None);
        }
    }

    /// A ferry's wagon lands as near as it can to the site it'll load at,
    /// and loads there if it lands on it.
    fn hauler_land(&mut self, w: usize, isle: u32, claims: &mut HashMap<usize, i64>) {
        let Some(k) = self.richest(isle, w, claims) else { return };
        let f = self.site_field[k].clone();
        self.land_toward(w, &f);
        if self.units[w].carrier == 0 && self.units[w].at == self.sites[k].at {
            self.load_site(w, k);
        }
    }

    /// A ferry's wagon on the landmass loads wheat at our sites there, and
    /// goes back aboard once it's nearly full, or once the ferry has waited
    /// long enough and it has some.
    fn hauler_island(&mut self, w: usize, t: usize, s: &Ship, lost: bool, claims: &mut HashMap<usize, i64>) {
        if self.units[w].fresh || self.units[w].mp <= 0 || !self.time_left() {
            return;
        }
        let room = self.rule("cargo_max") - self.units[w].wheat - self.units[w].bread;
        let site = if lost { None } else { self.richest(s.isle, w, claims) };
        if let Some(k) = site {
            if room >= 150 && !(s.waited >= FERRY_WAIT && self.units[w].wheat > 0) {
                if self.units[w].at != self.sites[k].at {
                    let f = self.site_field[k].clone();
                    self.walk(w, &f, true);
                }
                if self.units[w].at == self.sites[k].at {
                    self.load_site(w, k);
                }
                return;
            }
        }
        if let Some(path) = self.board_path(w, t) {
            self.go(w, &path);
            return;
        }
        let t_at = self.units[t].at;
        let goals: Vec<usize> = (0..6)
            .filter_map(|d| self.step(t_at, d))
            .filter(|&n| self.cost(n) > 0)
            .collect();
        if !goals.is_empty() {
            let f = self.field(&goals);
            self.walk(w, &f, true);
        }
    }

    /// A ferry's wagon lands in the port, unloads its wheat, and takes bread
    /// for the next trip.
    fn hauler_ashore(&mut self, w: usize, port: usize, isle: u32) {
        let f = self.city_field[port].clone();
        self.land_toward(w, &f);
        if self.units[w].carrier == 0 && self.units[w].at == self.cities[port].at {
            self.restock(w, port, isle);
        }
    }

    /// Idle land units stand guard: by our sites on a landmass only a ferry
    /// serves, where its wagons feed them, or by our cities at home.
    fn guard(&mut self) {
        let isles = self.haul_isles();
        let held = self.held_lands();
        let me = self.me;
        let mut fields: HashMap<u32, Vec<i32>> = HashMap::new();
        for u in 0..self.units.len() {
            let t = &self.types[self.units[u].ty];
            if !self.ready(u) || self.units[u].busy || self.units[u].mp <= 0 || t.carry || t.naval {
                continue;
            }
            if !self.time_left() {
                return;
            }
            let l = self.land[self.units[u].at];
            if !fields.contains_key(&l) {
                let mut goals: Vec<usize> = Vec::new();
                if isles.contains(&l) {
                    for k in self.isle_sites(l) {
                        let a = self.sites[k].at;
                        goals.push(a);
                        goals.extend((0..6).filter_map(|d| self.step(a, d)).filter(|&n| self.cost(n) > 0));
                    }
                } else if held.contains(&l) {
                    for c in self.cities.iter().filter(|c| c.owner == me && self.land[c.at] == l) {
                        goals.extend((0..6).filter_map(|d| self.step(c.at, d)).filter(|&n| self.cost(n) > 0));
                    }
                }
                let f = if goals.is_empty() { Vec::new() } else { self.field(&goals) };
                fields.insert(l, f);
            }
            let f = fields[&l].clone();
            if !f.is_empty() && f[self.units[u].at] > 0 {
                self.walk(u, &f, false);
            }
        }
    }

    /// How many transports and galleys to keep, and the seas worth building
    /// them on: those with somewhere to sail for, those by a landmass only
    /// a ferry can haul from, and those with enemy ports or ships.
    fn plan_ships(&mut self) {
        let me = self.me;
        let round = self.round;
        let (mut useful, mut targets) = (Vec::new(), false);
        let isles = self.haul_isles();
        let ports: Vec<usize> = (0..self.cities.len()).filter(|&c| self.cities[c].owner == me).collect();
        for c in ports {
            let home = self.land[self.cities[c].at];
            let mut seas: Vec<u32> = (0..6)
                .filter_map(|d| self.step(self.cities[c].at, d))
                .filter(|&n| self.naval_cost(n) > 0)
                .map(|n| self.sea[n])
                .collect();
            seas.sort_unstable();
            seas.dedup();
            for sea in seas {
                let Some(&dock) = self.port_water(c, sea).first() else { continue };
                if self.best_target(dock, home, 1e9, NONE).is_some() {
                    targets = true;
                    useful.push(sea);
                }
                for &isle in &isles {
                    if let Some(&k) = self.isle_sites(isle).first() {
                        if self.landings(self.sites[k].at).iter().any(|&w| self.sea[w] == sea) {
                            useful.push(sea);
                        }
                    }
                }
                let enemy_port = (0..self.cities.len())
                    .any(|e| self.cities[e].owner != me && self.cities[e].owner != 0 && !self.port_water(e, sea).is_empty());
                if enemy_port {
                    useful.push(sea);
                }
            }
        }
        useful.sort_unstable();
        useful.dedup();
        self.useful = useful;
        // Land units with nothing to do where we hold cities, beyond the
        // garrisons, could all sail: a transport for every four.
        let held = self.held_lands();
        let mut idle = self.crew.len() as i64
            + (0..self.units.len())
                .filter(|&u| {
                    let x = &self.units[u];
                    self.ready(u)
                        && !x.busy
                        && !self.types[x.ty].carry
                        && !self.types[x.ty].naval
                        && held.contains(&self.land[x.at])
                })
                .count() as i64;
        for c in self.cities.iter().filter(|c| c.owner == me) {
            if self.home_isles.contains(&self.land[c.at]) {
                idle -= GARRISON;
            }
        }
        self.want_t = if targets { 1 + idle.max(0) as usize / 4 } else { 0 };
        self.want_t = (self.want_t + isles.len()).min(TRANSPORTS_MAX);
        let seen = self.seen_ships.values().filter(|&&r| round - r <= 20).count();
        let enemy_ports = self.cities.iter().any(|c| c.owner != me && c.owner != 0);
        self.want_g = if self.galley.is_some() && (seen > 0 || (enemy_ports && self.want_t > 0)) {
            (1 + seen / 3).min(GALLEYS_MAX)
        } else {
            0
        };
    }

    // Building.

    /// Bread a turn: what our cities make, and our sites' wheat. A site on a
    /// landmass where we hold no city counts only while a ferry with wagons
    /// brings its wheat home.
    fn income(&self) -> i64 {
        let me = self.me;
        let held = self.held_lands();
        let ferried: Vec<u32> = self.haulers.values().copied().collect();
        let mut made = self.rule("city_bread") * self.cities.iter().filter(|c| c.owner == me).count() as i64;
        made += self
            .sites
            .iter()
            .filter(|s| s.owner == me)
            .filter(|s| !self.naval || held.contains(&self.land[s.at]) || ferried.contains(&self.land[s.at]))
            .map(|s| s.output)
            .sum::<i64>();
        made
    }

    fn build_all(&mut self) {
        let me = self.me;
        let mine: Vec<usize> = (0..self.units.len())
            .filter(|&u| self.units[u].alive && self.units[u].owner == me)
            .collect();
        let mut count: HashMap<char, f64> = HashMap::new();
        for &u in &mine {
            *count.entry(self.types[self.units[u].ty].code).or_insert(0.0) += 1.0;
        }
        let wagon = self.types.iter().position(|t| t.carry);
        // Units beyond what the bread income can feed would starve.
        let mut room = if self.supply {
            let eats = self.rule("eat_base") as f64 + 0.6 * self.rule("eat_move") as f64;
            (self.income() as f64 / (eats * self.rule("ration_cost").max(1) as f64)) as i64 - mine.len() as i64
        } else {
            i64::MAX
        };
        let hauls = self
            .sites
            .iter()
            .filter(|s| s.owner == me && !self.near_own_city(s.at))
            .count();
        // Each ferry wants two wagons of its own.
        let ferried = if self.naval { self.haul_isles().len() } else { 0 };
        let wagons_wanted = if self.supply {
            (1 + hauls / 2 + mine.len() / 25 + 2 * ferried).min(12) as f64
        } else {
            0.0
        };
        let theirs = self.enemy_cities();
        let front = if theirs.is_empty() {
            vec![0; self.tiles.len()]
        } else {
            self.field(&theirs)
        };
        let mut cities: Vec<usize> = (0..self.cities.len())
            .filter(|&c| self.cities[c].owner == me && !self.cities[c].built)
            .collect();
        cities.sort_by_key(|&c| (front[self.cities[c].at], c));
        let limit = self.rule("stack_limit").max(1) as u8;
        for c in cities {
            if room <= 0 || !self.time_left() {
                break;
            }
            let at = self.cities[c].at;
            if self.enemy[at] || self.friends[at] >= limit {
                continue;
            }
            let budget = if self.supply {
                self.cities[c].stock - KEEP
            } else {
                self.gold
            };
            // Ships first, at a port on a sea worth them. A port that wants
            // one and can't pay for it yet saves its bread.
            if let Some(t) = self.ship_wanted(c, budget, &count) {
                let code = self.types[t].code;
                let reply = self.order(&format!("B {c} {code}"));
                if reply[0] == "OK" {
                    *count.entry(code).or_insert(0.0) += 1.0;
                    room -= 1;
                    self.cities[c].built = true;
                    self.cities[c].stock -= self.types[t].cost;
                    continue;
                }
            } else if self.saving_for_ship(c, &count) {
                continue;
            }
            // The land army's shares leave out wagons and ships.
            let total: f64 = count
                .iter()
                .filter(|(k, _)| self.types.iter().any(|t| t.code == **k && !t.carry && !t.naval))
                .map(|(_, v)| v)
                .sum();
            let pick = if let (Some(w), true) = (
                wagon,
                *count.get(&self.types[wagon.unwrap_or(0)].code).unwrap_or(&0.0) < wagons_wanted,
            ) {
                Some(w)
            } else {
                // The type furthest below its share; capturers only, early on.
                (if self.supply { &MIX } else { &MIX_NO_SUPPLY })
                    .iter()
                    .filter_map(|&(code, share)| self.types.iter().position(|t| t.code == code).map(|t| (t, share)))
                    .filter(|&(t, _)| total >= 8.0 || self.types[t].capture)
                    .max_by(|a, b| {
                        let gap = |&(t, share): &(usize, f64)| {
                            share - count.get(&self.types[t].code).unwrap_or(&0.0) / total.max(1.0)
                        };
                        gap(a).total_cmp(&gap(b))
                    })
                    .map(|(t, _)| t)
            };
            let Some(mut t) = pick else { continue };
            if self.types[t].cost > budget {
                // Spend it anyway, on the cheapest capturer.
                let cheap = (0..self.types.len())
                    .filter(|&k| self.types[k].capture && self.types[k].cost <= budget)
                    .min_by_key(|&k| (self.types[k].cost, k));
                let Some(k) = cheap else { continue };
                t = k;
            }
            let code = self.types[t].code;
            let reply = self.order(&format!("B {c} {code}"));
            if reply[0] == "OK" {
                *count.entry(code).or_insert(0.0) += 1.0;
                room -= 1;
                self.cities[c].built = true;
                if self.supply {
                    self.cities[c].stock -= self.types[t].cost;
                } else {
                    self.gold -= self.types[t].cost;
                }
                self.friends[at] += 1;
            }
        }
    }

    /// The ship city c should build with its budget, if any: transports and
    /// galleys up to what the plan wants, at a port on a sea worth them.
    fn ship_wanted(&self, c: usize, budget: i64, count: &HashMap<char, f64>) -> Option<usize> {
        if !self.naval {
            return None;
        }
        let at = self.cities[c].at;
        let port = (0..6)
            .filter_map(|d| self.step(at, d))
            .any(|n| self.naval_cost(n) > 0 && self.useful.contains(&self.sea[n]) && !self.enemy[n]);
        if !port {
            return None;
        }
        let have = |t: usize| *count.get(&self.types[t].code).unwrap_or(&0.0) as usize;
        if let Some(t) = self.transport {
            if have(t) < self.want_t && budget >= self.types[t].cost {
                return Some(t);
            }
        }
        if let Some(g) = self.galley {
            if have(g) < self.want_g && budget >= self.types[g].cost {
                return Some(g);
            }
        }
        None
    }

    /// Whether city c saves its bread for a ship a port on its landmass
    /// wants and can't pay for yet. Its wagons carry the bread there. Cities
    /// still build while the landmass has less than its garrison.
    fn saving_for_ship(&self, c: usize, count: &HashMap<char, f64>) -> bool {
        if !self.naval {
            return false;
        }
        let me = self.me;
        let l = self.land[self.cities[c].at];
        let ours = self.cities.iter().filter(|x| x.owner == me && self.land[x.at] == l).count() as i64;
        let army = self
            .units
            .iter()
            .filter(|u| {
                u.alive
                    && u.owner == me
                    && u.carrier == 0
                    && self.land[u.at] == l
                    && !self.types[u.ty].carry
                    && !self.types[u.ty].naval
            })
            .count() as i64;
        army >= GARRISON * ours
            && (0..self.cities.len()).any(|p| {
                self.cities[p].owner == me
                    && self.land[self.cities[p].at] == l
                    && self.ship_wanted(p, i64::MAX, count).is_some()
                    && self.ship_wanted(p, self.cities[p].stock - KEEP, count).is_none()
            })
    }

    fn turn(&mut self) {
        self.capture_all();
        self.attack_all();
        if self.supply {
            self.feed();
            self.wagons();
        } else {
            self.food = vec![FAR; self.tiles.len()];
        }
        self.raid();
        self.hold();
        self.spread();
        self.sea();
        self.attack_all();
        self.capture_all();
        self.build_all();
    }
}

fn main() {
    let mut bot = Bot::new();
    bot.init();
    while let Some(line) = bot.read() {
        let t: Vec<&str> = line.split_whitespace().collect();
        match t.first().copied() {
            Some("TURN") => {
                bot.round = t.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
                bot.read_turn();
                bot.turn();
                let round = bot.round;
                bot.send(&format!("END {round}"));
            }
            Some("GAMEOVER") => return,
            _ => {}
        }
    }
}
