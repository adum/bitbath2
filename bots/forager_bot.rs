// Forager: a simple bot in Rust, built for the strategic ruleset
// (STRATEGIC.md) but able to play the default game too. Where the
// reference bot masses its army on one enemy city, Forager spreads out and
// goes after the enemy's supply:
//
// - Every capturer heads for the nearest city or site no other unit is
//   after, so it takes ground fast.
// - Cavalry and scouts raid: they hunt enemy wagons and stand on enemy
//   sites, which stops them growing wheat, or on enemy cities, which stops
//   them building.
// - Pikemen hold the cities nearest the enemy and dig in.
// - A unit attacks only when it takes more value than it loses back, with
//   no planned kills.
// - Wagons haul wheat from the sites that don't deliver by themselves, one
//   wagon in three carries bread to hungry units, and hungry units walk to
//   bread.
// - It builds a fixed mix of unit types with each city's own bread (or with
//   gold in the default game), and no more units than its bread can feed.
//
// It has no dependencies. Build: rustc -O bots/forager_bot.rs -o target/release/forager_bot.exe

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::time::Instant;

const DIRS: [(i32, i32); 6] = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)];
const FAR: i32 = 1 << 28;
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

fn dist(a: (i32, i32), b: (i32, i32)) -> i32 {
    let (dq, dr) = (a.0 - b.0, a.1 - b.1);
    (dq.abs() + dr.abs() + (dq + dr).abs()) / 2
}

struct Terrain {
    move_cost: i32,
    defense: i32,
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
}

#[derive(Clone)]
struct Unit {
    id: u32,
    owner: u8,
    ty: usize,
    at: usize,
    hp: i32,
    fort: i32,
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
        self.jobs.retain(|id, _| self.units.iter().any(|u| u.id == *id));
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

    /// Recounts who stands where, and the enemy's zones of control.
    fn mark(&mut self) {
        let n = self.tiles.len();
        self.friends = vec![0; n];
        self.enemy = vec![false; n];
        self.zoc = vec![false; n];
        for u in self.units.iter().filter(|u| u.alive) {
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

    /// Where unit `u` can end a move this turn, with the path there.
    fn reach(&self, u: usize) -> Vec<(usize, String)> {
        let unit = &self.units[u];
        let mut out = vec![(unit.at, String::new())];
        if unit.mp <= 0 || unit.acted || unit.fresh {
            return out;
        }
        let skirmish = self.types[unit.ty].skirmish;
        let limit = self.rule("stack_limit").max(1) as u8;
        let mut best: HashMap<usize, (i32, String)> = HashMap::new();
        best.insert(unit.at, (0, String::new()));
        let mut heap = BinaryHeap::new();
        heap.push(Reverse((0, unit.at)));
        while let Some(Reverse((c, i))) = heap.pop() {
            let path = best[&i].1.clone();
            if c > best[&i].0 {
                continue;
            }
            if i != unit.at {
                if self.friends[i] < limit {
                    out.push((i, path.clone()));
                }
                if !skirmish && self.zoc[i] {
                    continue;
                }
            }
            for d in 0..6 {
                let Some(n) = self.step(i, d) else { continue };
                let enter = self.cost(n);
                if enter == 0 || self.enemy[n] || c + enter > unit.mp {
                    continue;
                }
                if best.get(&n).is_none_or(|b| c + enter < b.0) {
                    best.insert(n, (c + enter, format!("{path}{d}")));
                    heap.push(Reverse((c + enter, n)));
                }
            }
        }
        out
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
            self.units[u].at = at;
            self.units[u].mp = if t[0] == "OK" { t[3].parse().unwrap_or(0) } else { 0 };
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
        let (a, d) = (&self.units[u], &self.units[e]);
        let range = self.types[a.ty].range;
        let gap = self.hexdist(a.at, d.at);
        if gap < range.0
            || gap > range.1
            || self.damage[a.ty][d.ty] == 0
            || (self.types[a.ty].no_move_attack && a.moved)
        {
            return None;
        }
        let hit = self.dmg(a.ty, a.hp, d.ty, d.at, d.fort).min(d.hp);
        let mut gain = self.value(d, hit);
        let left = d.hp - hit;
        if left <= 0 {
            gain += self.types[d.ty].cost as f64 * 0.5;
        }
        let back_range = self.types[d.ty].range;
        let mut loss = 0.0;
        if left > 0 && gap >= back_range.0 && gap <= back_range.1 {
            let back = self.dmg(d.ty, left, a.ty, a.at, a.fort).min(a.hp);
            loss = self.value(a, back);
            if back >= a.hp {
                loss += self.types[a.ty].cost as f64 * 0.5;
            }
        }
        Some(gain - loss)
    }

    fn ready(&self, u: usize) -> bool {
        let x = &self.units[u];
        x.alive && x.owner == self.me && !x.acted && !x.fresh
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
        let haul: Vec<usize> = (0..self.sites.len())
            .filter(|&s| {
                self.sites[s].owner == me && !self.enemy[self.sites[s].at] && !self.near_own_city(self.sites[s].at)
            })
            .collect();
        let hungry: Vec<usize> = (0..self.units.len())
            .filter(|&u| {
                let x = &self.units[u];
                x.alive
                    && x.owner == me
                    && !self.types[x.ty].carry
                    && x.rations <= self.rule("ration_max") as i32 / 2
                    && self.food[x.at] > 0
            })
            .collect();
        let wagons: Vec<usize> = (0..self.units.len())
            .filter(|&u| self.ready(u) && self.types[self.units[u].ty].carry)
            .collect();
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
            let raider = !t.capture && !t.carry && t.range.1 <= 1 && (self.supply || t.skirmish);
            if !self.ready(u) || self.units[u].busy || !raider {
                continue;
            }
            if !self.time_left() {
                return;
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
            if self.ready(u) && !self.units[u].busy && !self.types[self.units[u].ty].carry && self.time_left() {
                self.walk(u, &f, false);
            }
        }
    }

    // Building.

    fn income(&self) -> i64 {
        let me = self.me;
        let mut made = self.rule("city_bread") * self.cities.iter().filter(|c| c.owner == me).count() as i64;
        made += self
            .sites
            .iter()
            .filter(|s| s.owner == me)
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
        let wagons_wanted = if self.supply {
            (1 + hauls / 2 + mine.len() / 25).min(12) as f64
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
            let total: f64 = count
                .iter()
                .filter(|(k, _)| Some(**k) != wagon.map(|w| self.types[w].code))
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
