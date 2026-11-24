// ApexBot v2: Exact gold-trade economy, focus-fire kill-order optimization,
// smart expansion, forest ambush & scout spotting, and frontline concentration.
// Compile: rustc -O bots/apex_bot.rs -o target/release/apex_bot.exe

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::time::Instant;

const DIRS: [(i32, i32); 6] = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)];
const INF_DIST: i32 = 1_000_000;
const TIME_MARGIN_MS: u128 = 40;

#[inline]
fn hex_dist(q1: i32, r1: i32, q2: i32, r2: i32) -> i32 {
    let dq = q1 - q2;
    let dr = r1 - r2;
    (dq.abs() + dr.abs() + (dq + dr).abs()) / 2
}

#[derive(Clone, Debug)]
struct Terrain {
    move_cost: i32,
    defense: i32,
    vision_bonus: i32,
    conceal: i32,
}

#[derive(Clone, Debug)]
struct UnitType {
    code: String,
    cost: i32,
    max_hp: i32,
    mv: i32,
    vision: i32,
    range_min: i32,
    range_max: i32,
    capture: bool,
    zoc: bool,
    skirmish: bool,
    no_move_attack: bool,
}

impl UnitType {
    #[inline]
    fn in_range(&self, d: i32) -> bool {
        d >= self.range_min && d <= self.range_max
    }
}

#[derive(Clone, Debug)]
struct City {
    id: usize,
    q: i32,
    r: i32,
    idx: usize,
    owner: i32,
    capturer: i32,
    progress: i32,
    built: bool,
}

#[derive(Clone, Debug)]
struct Unit {
    id: u32,
    owner: i32,
    utype: usize,
    q: i32,
    r: i32,
    idx: usize,
    hp: i32,
    mp: i32,
    acted: bool,
    moved: bool,
    fresh: bool,
}

#[derive(Clone, Debug)]
struct Ghost {
    id: u32,
    utype: usize,
    idx: usize,
    hp: i32,
    seen_round: i32,
}

struct Reply {
    ok: bool,
    kind: String,
    values: Vec<String>,
}

struct Bot {
    player: i32,
    width: i32,
    height: i32,
    ncells: usize,

    terrains: HashMap<char, Terrain>,
    unit_types: Vec<UnitType>,
    type_by_code: HashMap<String, usize>,
    damage_table: Vec<Vec<i32>>,
    constants: HashMap<String, i32>,

    // Map cell arrays
    cell_q: Vec<i32>,
    cell_r: Vec<i32>,
    cell_passable: Vec<bool>,
    cell_move_cost: Vec<i32>,
    cell_defense: Vec<i32>,
    cell_vbonus: Vec<i32>,
    cell_conceal: Vec<i32>,
    nbrs: Vec<Vec<(u8, usize, i32)>>,
    city_at: Vec<Option<usize>>,

    // Precomputed city info
    init_cities: Vec<(usize, i32, i32, i32)>,
    city_dist: Vec<Vec<i32>>,
    city_my_home_dist: Vec<i32>,
    city_enemy_home_dist: Vec<i32>,

    // Rule constants cached
    stack_limit: i32,
    capture_points: i32,
    round_limit: i32,
    city_income: i32,
    unit_upkeep: i32,
    city_vision: i32,

    // Persistent memory across turns
    ghosts: HashMap<u32, Ghost>,
    seen_enemy_types: HashMap<u32, usize>,
    par_trade: f64,

    // Turn state
    round: i32,
    time_ms: u128,
    turn_start: Instant,
    gold: i32,
    cities: Vec<City>,
    units: HashMap<u32, Unit>,
    my_unit_ids: Vec<u32>,
    enemy_unit_ids: Vec<u32>,

    // Derived per-turn / synced caches
    occ: Vec<i32>,
    enemy_occ: Vec<i32>,
    enemy_zoc: Vec<bool>,
    support: Vec<f64>,
    threat: Vec<f64>,
    enemy_striker_cnt: Vec<f64>,
    menace: Vec<Vec<f64>>, // [my_utype][cell] -> weighted threat
    failed_move: HashSet<u32>,
    loss_weight: f64,
    expose_weight: f64,
    risk_weight: f64,
    last_turn: bool,
    debug_count: i32,

    reader: BufReader<io::Stdin>,
    writer: BufWriter<io::Stdout>,
}

impl Bot {
    fn new() -> Self {
        Bot {
            player: 1,
            width: 0,
            height: 0,
            ncells: 0,
            terrains: HashMap::new(),
            unit_types: Vec::new(),
            type_by_code: HashMap::new(),
            damage_table: Vec::new(),
            constants: HashMap::new(),
            cell_q: Vec::new(),
            cell_r: Vec::new(),
            cell_passable: Vec::new(),
            cell_move_cost: Vec::new(),
            cell_defense: Vec::new(),
            cell_vbonus: Vec::new(),
            cell_conceal: Vec::new(),
            nbrs: Vec::new(),
            city_at: Vec::new(),
            init_cities: Vec::new(),
            city_dist: Vec::new(),
            city_my_home_dist: Vec::new(),
            city_enemy_home_dist: Vec::new(),
            stack_limit: 4,
            capture_points: 200,
            round_limit: 200,
            city_income: 100,
            unit_upkeep: 5,
            city_vision: 2,
            ghosts: HashMap::new(),
            seen_enemy_types: HashMap::new(),
            par_trade: 30.0,
            round: 0,
            time_ms: 5000,
            turn_start: Instant::now(),
            gold: 0,
            cities: Vec::new(),
            units: HashMap::new(),
            my_unit_ids: Vec::new(),
            enemy_unit_ids: Vec::new(),
            occ: Vec::new(),
            enemy_occ: Vec::new(),
            enemy_zoc: Vec::new(),
            support: Vec::new(),
            threat: Vec::new(),
            enemy_striker_cnt: Vec::new(),
            menace: Vec::new(),
            failed_move: HashSet::new(),
            loss_weight: 0.80,
            expose_weight: 12.0,
            risk_weight: 12.0,
            last_turn: false,
            debug_count: 0,
            reader: BufReader::new(io::stdin()),
            writer: BufWriter::new(io::stdout()),
        }
    }

    #[inline]
    fn to_idx(&self, q: i32, r: i32) -> Option<usize> {
        if r < 0 || r >= self.height {
            return None;
        }
        let col = q + r.div_euclid(2);
        if col < 0 || col >= self.width {
            return None;
        }
        Some((r * self.width + col) as usize)
    }

    #[inline]
    fn cell_dist(&self, a: usize, b: usize) -> i32 {
        hex_dist(self.cell_q[a], self.cell_r[a], self.cell_q[b], self.cell_r[b])
    }

    fn read_line(&mut self) -> Option<String> {
        let mut s = String::new();
        match self.reader.read_line(&mut s) {
            Ok(0) => None,
            Ok(_) => {
                while s.ends_with('\n') || s.ends_with('\r') {
                    s.pop();
                }
                Some(s)
            }
            Err(_) => None,
        }
    }

    fn send_line(&mut self, line: &str) {
        let _ = writeln!(self.writer, "{}", line);
        let _ = self.writer.flush();
    }

    fn debug_line(&mut self, a: usize, b: usize, color: &str) {
        if self.debug_count < 300 {
            self.debug_count += 1;
            let _ = writeln!(
                self.writer,
                "D LINE {} {} {} {} {}",
                self.cell_q[a], self.cell_r[a], self.cell_q[b], self.cell_r[b], color
            );
        }
    }

    fn debug_text(&mut self, text: &str) {
        if self.debug_count < 350 {
            self.debug_count += 1;
            let _ = writeln!(self.writer, "D TEXT {}", text);
        }
    }

    #[inline]
    fn out_of_time(&self) -> bool {
        self.turn_start.elapsed().as_millis() + TIME_MARGIN_MS >= self.time_ms
    }

    fn send_order(&mut self, line: &str) -> Option<Reply> {
        if self.out_of_time() {
            return Some(Reply {
                ok: false,
                kind: "ERR".to_string(),
                values: vec!["late".to_string()],
            });
        }
        self.send_line(line);
        loop {
            let reply = self.read_line()?;
            let mut parts = reply.split_whitespace();
            let kw = match parts.next() {
                Some(k) => k,
                None => continue,
            };
            if kw == "SEE" {
                let id: u32 = parts.next().unwrap().parse().unwrap();
                let owner: i32 = parts.next().unwrap().parse().unwrap();
                let code = parts.next().unwrap();
                let q: i32 = parts.next().unwrap().parse().unwrap();
                let r: i32 = parts.next().unwrap().parse().unwrap();
                let hp: i32 = parts.next().unwrap().parse().unwrap();
                if let (Some(&utype), Some(idx)) = (self.type_by_code.get(code), self.to_idx(q, r)) {
                    self.units.insert(
                        id,
                        Unit {
                            id,
                            owner,
                            utype,
                            q,
                            r,
                            idx,
                            hp,
                            mp: 0,
                            acted: false,
                            moved: false,
                            fresh: false,
                        },
                    );
                    if owner != self.player {
                        self.seen_enemy_types.insert(id, utype);
                    }
                }
            } else if kw == "OK" || kw == "PART" || kw == "ERR" {
                let values: Vec<String> = parts.map(|s| s.to_string()).collect();
                return Some(Reply {
                    ok: kw == "OK" || kw == "PART",
                    kind: kw.to_string(),
                    values,
                });
            } else if kw == "GAMEOVER" {
                return None;
            }
        }
    }

    fn parse_init(&mut self) -> bool {
        let mut rows: Vec<String> = Vec::new();
        let mut raw_damages: Vec<(String, Vec<i32>)> = Vec::new();
        loop {
            let line = match self.read_line() {
                Some(l) => l,
                None => return false,
            };
            let t: Vec<&str> = line.split_whitespace().collect();
            if t.is_empty() {
                continue;
            }
            match t[0] {
                "INIT" => break,
                "PLAYER" => self.player = t[1].parse().unwrap_or(1),
                "MAP" => {
                    self.width = t[1].parse().unwrap();
                    self.height = t[2].parse().unwrap();
                }
                "ROW" => rows.push(t[1].to_string()),
                "CITY" => {
                    let id: usize = t[1].parse().unwrap();
                    let q: i32 = t[2].parse().unwrap();
                    let r: i32 = t[3].parse().unwrap();
                    let owner: i32 = t[4].parse().unwrap();
                    self.init_cities.push((id, q, r, owner));
                }
                "TERRAIN" => {
                    let ch = t[1].chars().next().unwrap();
                    let move_cost: i32 = t[3].parse().unwrap();
                    let defense: i32 = t[4].parse().unwrap();
                    let vision_bonus: i32 = t[5].parse().unwrap();
                    let conceal: i32 = t.get(6).and_then(|s| s.parse().ok()).unwrap_or(0);
                    self.terrains.insert(
                        ch,
                        Terrain {
                            move_cost,
                            defense: defense.min(100),
                            vision_bonus,
                            conceal,
                        },
                    );
                }
                "UNITTYPE" => {
                    let code = t[1].to_string();
                    let cost: i32 = t[3].parse().unwrap();
                    let max_hp: i32 = t[4].parse().unwrap();
                    let mv: i32 = t[5].parse().unwrap();
                    let vision: i32 = t[6].parse().unwrap();
                    let range_min: i32 = t[7].parse().unwrap();
                    let range_max: i32 = t[8].parse().unwrap();
                    let flags_str = t[9];
                    let mut capture = false;
                    let mut zoc = false;
                    let mut skirmish = false;
                    let mut no_move_attack = false;
                    if flags_str != "-" {
                        for f in flags_str.split(',') {
                            match f {
                                "capture" => capture = true,
                                "zoc" => zoc = true,
                                "skirmish" => skirmish = true,
                                "no_move_attack" => no_move_attack = true,
                                _ => {}
                            }
                        }
                    }
                    let idx = self.unit_types.len();
                    self.type_by_code.insert(code.clone(), idx);
                    self.unit_types.push(UnitType {
                        code,
                        cost,
                        max_hp,
                        mv,
                        vision,
                        range_min,
                        range_max,
                        capture,
                        zoc,
                        skirmish,
                        no_move_attack,
                    });
                }
                "DAMAGE" => {
                    let code = t[1].to_string();
                    let vals: Vec<i32> = t[2..].iter().map(|s| s.parse().unwrap()).collect();
                    raw_damages.push((code, vals));
                }
                "RULE" => {
                    let k = t[1].to_string();
                    let v: i32 = t[2].parse().unwrap();
                    self.constants.insert(k, v);
                }
                _ => {}
            }
        }

        let nt = self.unit_types.len();
        self.damage_table = vec![vec![0; nt]; nt];
        for (code, vals) in raw_damages {
            if let Some(&a) = self.type_by_code.get(&code) {
                for (b, &v) in vals.iter().enumerate() {
                    if b < nt {
                        self.damage_table[a][b] = v;
                    }
                }
            }
        }

        self.stack_limit = *self.constants.get("stack_limit").unwrap_or(&4);
        self.capture_points = *self.constants.get("capture_points").unwrap_or(&200);
        self.round_limit = *self.constants.get("round_limit").unwrap_or(&200);
        self.city_income = *self.constants.get("city_income").unwrap_or(&100);
        self.unit_upkeep = *self.constants.get("unit_upkeep").unwrap_or(&5);
        self.city_vision = *self.constants.get("city_vision").unwrap_or(&2);

        self.ncells = (self.width * self.height) as usize;
        self.cell_q = vec![0; self.ncells];
        self.cell_r = vec![0; self.ncells];
        self.cell_passable = vec![false; self.ncells];
        self.cell_move_cost = vec![0; self.ncells];
        self.cell_defense = vec![0; self.ncells];
        self.cell_vbonus = vec![0; self.ncells];
        self.cell_conceal = vec![0; self.ncells];
        self.city_at = vec![None; self.ncells];

        for r in 0..self.height {
            let row_chars: Vec<char> = rows[r as usize].chars().collect();
            for col in 0..self.width {
                let idx = (r * self.width + col) as usize;
                let q = col - r.div_euclid(2);
                self.cell_q[idx] = q;
                self.cell_r[idx] = r;
                let ch = row_chars[col as usize];
                if let Some(ter) = self.terrains.get(&ch) {
                    self.cell_passable[idx] = ter.move_cost > 0;
                    self.cell_move_cost[idx] = ter.move_cost;
                    self.cell_defense[idx] = ter.defense;
                    self.cell_vbonus[idx] = ter.vision_bonus;
                    self.cell_conceal[idx] = ter.conceal;
                }
            }
        }

        self.nbrs = vec![Vec::new(); self.ncells];
        for idx in 0..self.ncells {
            if !self.cell_passable[idx] {
                continue;
            }
            let q = self.cell_q[idx];
            let r = self.cell_r[idx];
            for d in 0..6 {
                let nq = q + DIRS[d].0;
                let nr = r + DIRS[d].1;
                if let Some(nidx) = self.to_idx(nq, nr) {
                    if self.cell_passable[nidx] {
                        self.nbrs[idx].push((d as u8, nidx, self.cell_move_cost[nidx]));
                    }
                }
            }
        }

        self.init_cities.sort_by_key(|c| c.0);
        for &(cid, q, r, _) in &self.init_cities {
            if let Some(idx) = self.to_idx(q, r) {
                self.city_at[idx] = Some(cid);
            }
        }

        self.city_dist = Vec::with_capacity(self.init_cities.len());
        for &(_, q, r, _) in &self.init_cities {
            let goal = self.to_idx(q, r).unwrap();
            self.city_dist.push(self.compute_dist_field(&[goal]));
        }

        let my_homes: Vec<usize> = self
            .init_cities
            .iter()
            .filter(|c| c.3 == self.player)
            .map(|c| self.to_idx(c.1, c.2).unwrap())
            .collect();
        let enemy_homes: Vec<usize> = self
            .init_cities
            .iter()
            .filter(|c| c.3 != 0 && c.3 != self.player)
            .map(|c| self.to_idx(c.1, c.2).unwrap())
            .collect();
        self.city_my_home_dist = self
            .init_cities
            .iter()
            .map(|c| {
                my_homes
                    .iter()
                    .map(|&h| self.city_dist[c.0][h])
                    .min()
                    .unwrap_or(0)
            })
            .collect();
        self.city_enemy_home_dist = self
            .init_cities
            .iter()
            .map(|c| {
                enemy_homes
                    .iter()
                    .map(|&h| self.city_dist[c.0][h])
                    .min()
                    .unwrap_or(0)
            })
            .collect();

        let line_ut = (0..nt)
            .filter(|&i| self.unit_types[i].capture)
            .min_by_key(|&i| self.unit_types[i].cost)
            .unwrap_or(0);
        if nt > 0 {
            let par = self.first_strike_val(line_ut, self.unit_types[line_ut].max_hp, line_ut, 0);
            self.par_trade = if par > 1.0 { par } else { 25.0 };
        }

        self.occ = vec![0; self.ncells];
        self.enemy_occ = vec![0; self.ncells];
        self.enemy_zoc = vec![false; self.ncells];
        self.support = vec![0.0; self.ncells];
        self.threat = vec![0.0; self.ncells];
        self.enemy_striker_cnt = vec![0.0; self.ncells];
        self.menace = vec![vec![0.0; self.ncells]; nt];

        self.send_line("READY");
        true
    }

    fn compute_dist_field(&self, goals: &[usize]) -> Vec<i32> {
        let mut dist = vec![INF_DIST; self.ncells];
        let mut heap: BinaryHeap<Reverse<(i32, usize)>> = BinaryHeap::new();
        for &g in goals {
            if self.cell_passable[g] {
                dist[g] = 0;
                heap.push(Reverse((0, g)));
            }
        }
        while let Some(Reverse((cost, h))) = heap.pop() {
            if cost > dist[h] {
                continue;
            }
            let enter_h = self.cell_move_cost[h];
            for &(_, n, _) in &self.nbrs[h] {
                let c = cost + enter_h;
                if c < dist[n] {
                    dist[n] = c;
                    heap.push(Reverse((c, n)));
                }
            }
        }
        dist
    }

    #[inline]
    fn damage(&self, att_ut: usize, att_hp: i32, def_ut: usize, def_cell: usize) -> i32 {
        let base = self.damage_table[att_ut][def_ut];
        if base == 0 {
            return 0;
        }
        let def = self.cell_defense[def_cell];
        let max_hp = self.unit_types[att_ut].max_hp;
        ((base * att_hp * (100 - def)) / (max_hp * 100)).max(1)
    }

    fn first_strike_val(&self, att_ut: usize, att_hp: i32, def_ut: usize, def_pct: i32) -> f64 {
        let a = &self.unit_types[att_ut];
        let d = &self.unit_types[def_ut];
        let base = self.damage_table[att_ut][def_ut];
        if base == 0 {
            return 0.0;
        }
        let hit = ((base * att_hp * (100 - def_pct)) / (a.max_hp * 100))
            .max(1)
            .min(d.max_hp);
        let gain = (d.cost as f64) * (hit as f64) / (d.max_hp as f64);
        let mut loss = 0.0;
        if hit < d.max_hp && d.in_range(a.range_min) && self.damage_table[def_ut][att_ut] > 0 {
            let back = ((self.damage_table[def_ut][att_ut] * (d.max_hp - hit)) / d.max_hp)
                .max(1)
                .min(att_hp);
            loss = (a.cost as f64) * (back as f64) / (a.max_hp as f64);
        }
        (gain - 0.8 * loss).max(0.0)
    }

    #[inline]
    fn unit_worth(&self, utype: usize, hp: i32) -> f64 {
        let t = &self.unit_types[utype];
        (t.cost as f64) * (hp as f64) / (t.max_hp as f64)
    }

    fn refresh_caches(&mut self) {
        self.occ.fill(0);
        self.enemy_occ.fill(0);
        self.enemy_zoc.fill(false);
        self.support.fill(0.0);
        self.threat.fill(0.0);
        self.my_unit_ids.clear();
        self.enemy_unit_ids.clear();

        let mut my_list = Vec::new();
        let mut en_list = Vec::new();
        for u in self.units.values() {
            if u.owner == self.player {
                my_list.push((u.id, u.idx, u.utype, u.hp));
            } else {
                en_list.push((u.id, u.idx, u.utype, u.hp));
            }
        }
        my_list.sort_unstable_by_key(|x| x.0);
        en_list.sort_unstable_by_key(|x| x.0);

        for &(id, idx, ut, hp) in &my_list {
            self.my_unit_ids.push(id);
            self.occ[idx] += 1;
            let w = self.unit_worth(ut, hp);
            let q0 = self.cell_q[idx];
            let r0 = self.cell_r[idx];
            for dr in -2..=2 {
                for dq in (-2i32).max(-dr - 2)..=2i32.min(-dr + 2) {
                    if let Some(c) = self.to_idx(q0 + dq, r0 + dr) {
                        self.support[c] += w;
                    }
                }
            }
        }

        for &(id, idx, ut, hp) in &en_list {
            self.enemy_unit_ids.push(id);
            self.enemy_occ[idx] += 1;
            if self.unit_types[ut].zoc {
                for &(_, n, _) in &self.nbrs[idx] {
                    self.enemy_zoc[n] = true;
                }
            }
            let w = self.unit_worth(ut, hp);
            let q0 = self.cell_q[idx];
            let r0 = self.cell_r[idx];
            for dr in -3..=3 {
                for dq in (-3i32).max(-dr - 3)..=3i32.min(-dr + 3) {
                    if let Some(c) = self.to_idx(q0 + dq, r0 + dr) {
                        self.threat[c] += w;
                    }
                }
            }
        }
    }

    fn compute_visibility(&self, include_scouts: bool) -> Vec<bool> {
        let mut vis = vec![false; self.ncells];
        let mut mark = |origin: usize, vision: i32| {
            let q0 = self.cell_q[origin];
            let r0 = self.cell_r[origin];
            for dr in -vision..=vision {
                for dq in (-vision).max(-dr - vision)..=vision.min(-dr + vision) {
                    if let Some(c) = self.to_idx(q0 + dq, r0 + dr) {
                        let d = (dq.abs() + dr.abs() + (dq + dr).abs()) / 2;
                        if d <= 1 || d + self.cell_conceal[c] <= vision {
                            vis[c] = true;
                        }
                    }
                }
            }
        };
        for c in &self.cities {
            if c.owner == self.player {
                mark(c.idx, self.city_vision);
            }
        }
        for &uid in &self.my_unit_ids {
            let u = &self.units[&uid];
            let t = &self.unit_types[u.utype];
            if !include_scouts && t.skirmish && !t.capture {
                continue;
            }
            let v = t.vision + self.cell_vbonus[u.idx];
            mark(u.idx, v);
        }
        vis
    }

    fn compute_threat_maps(&mut self) {
        self.enemy_striker_cnt.fill(0.0);
        for row in &mut self.menace {
            row.fill(0.0);
        }

        let mut my_zoc = vec![false; self.ncells];
        for &uid in &self.my_unit_ids {
            let u = &self.units[&uid];
            if self.unit_types[u.utype].zoc {
                for &(_, n, _) in &self.nbrs[u.idx] {
                    my_zoc[n] = true;
                }
            }
        }

        let mut threats: Vec<(usize, usize, i32, f64)> = Vec::new();
        for &eid in &self.enemy_unit_ids {
            let e = &self.units[&eid];
            threats.push((e.idx, e.utype, e.hp, 1.0));
        }
        for g in self.ghosts.values() {
            if !self.units.contains_key(&g.id) {
                threats.push((g.idx, g.utype, g.hp, 1.0));
            }
        }

        let nt = self.unit_types.len();
        let mut dist = vec![INF_DIST; self.ncells];
        let mut touched = Vec::new();
        let mut struck = vec![false; self.ncells];
        let mut struck_list = Vec::new();

        for (origin, utype, hp, weight) in threats {
            let t = &self.unit_types[utype];
            let budget = if t.no_move_attack { 0 } else { t.mv };
            let mut heap: BinaryHeap<Reverse<(i32, usize)>> = BinaryHeap::new();
            dist[origin] = 0;
            touched.push(origin);
            heap.push(Reverse((0, origin)));

            while let Some(Reverse((cost, h))) = heap.pop() {
                if cost > dist[h] {
                    continue;
                }
                if h != origin && !t.skirmish && my_zoc[h] {
                    continue;
                }
                for &(_, n, enter) in &self.nbrs[h] {
                    let c = cost + enter;
                    if c <= budget && c < dist[n] && self.occ[n] == 0 {
                        if dist[n] == INF_DIST {
                            touched.push(n);
                        }
                        dist[n] = c;
                        heap.push(Reverse((c, n)));
                    }
                }
            }

            for &h in &touched {
                let q0 = self.cell_q[h];
                let r0 = self.cell_r[h];
                let rmax = t.range_max;
                let rmin = t.range_min;
                for dr in -rmax..=rmax {
                    for dq in (-rmax).max(-dr - rmax)..=rmax.min(-dr + rmax) {
                        let d = (dq.abs() + dr.abs() + (dq + dr).abs()) / 2;
                        if d >= rmin && d <= rmax {
                            if let Some(c) = self.to_idx(q0 + dq, r0 + dr) {
                                if !struck[c] {
                                    struck[c] = true;
                                    struck_list.push(c);
                                }
                            }
                        }
                    }
                }
            }

            let hp_frac = (hp as f64 / t.max_hp as f64) * weight;
            let mut men_by_ut = vec![0.0; nt];
            for my_ut in 0..nt {
                men_by_ut[my_ut] =
                    (self.first_strike_val(utype, hp, my_ut, 0) / self.par_trade) * weight;
            }

            for &c in &struck_list {
                self.enemy_striker_cnt[c] += hp_frac;
                for my_ut in 0..nt {
                    if men_by_ut[my_ut] > 0.0 {
                        self.menace[my_ut][c] += men_by_ut[my_ut];
                    }
                }
                struck[c] = false;
            }
            struck_list.clear();
            for &h in &touched {
                dist[h] = INF_DIST;
            }
            touched.clear();
        }
    }

    #[inline]
    fn risk_at(&self, cell: usize, my_ut: usize) -> f64 {
        self.menace[my_ut][cell] * (1.0 - self.cell_defense[cell] as f64 / 100.0)
    }

    fn reachable(&self, uid: u32, block_foreign_cities: bool) -> HashMap<usize, String> {
        let mut out = HashMap::new();
        let u = match self.units.get(&uid) {
            Some(u) => u,
            None => return out,
        };
        out.insert(u.idx, String::new());
        if u.mp <= 0 || u.acted || u.fresh {
            return out;
        }
        let t = &self.unit_types[u.utype];
        let avoid_foreign = block_foreign_cities && !t.capture;
        let mut best: HashMap<usize, i32> = HashMap::new();
        let mut heap: BinaryHeap<Reverse<(i32, usize, String)>> = BinaryHeap::new();
        best.insert(u.idx, 0);
        heap.push(Reverse((0, u.idx, String::new())));

        while let Some(Reverse((cost, h, path))) = heap.pop() {
            if cost > *best.get(&h).unwrap_or(&INF_DIST) {
                continue;
            }
            if h != u.idx {
                let is_foreign_city = self
                    .city_at[h]
                    .map(|cid| self.cities[cid].owner != self.player)
                    .unwrap_or(false);
                if self.occ[h] < self.stack_limit && !(avoid_foreign && is_foreign_city) {
                    out.insert(h, path.clone());
                }
                if !t.skirmish && self.enemy_zoc[h] {
                    continue;
                }
            }
            for &(d, n, enter) in &self.nbrs[h] {
                let c = cost + enter;
                if c <= u.mp && c < *best.get(&n).unwrap_or(&INF_DIST) && self.enemy_occ[n] == 0 {
                    best.insert(n, c);
                    let mut np = path.clone();
                    np.push((b'0' + d) as char);
                    heap.push(Reverse((c, n, np)));
                }
            }
        }
        out
    }

    fn do_move(&mut self, uid: u32, dirs: &str) -> Option<bool> {
        if dirs.is_empty() {
            return Some(false);
        }
        let prev_units = self.units.len();
        let old_idx = self.units[&uid].idx;
        let cmd = format!("M {} {}", uid, dirs);
        let reply = self.send_order(&cmd)?;
        if reply.ok {
            let q: i32 = reply.values[0].parse().unwrap();
            let r: i32 = reply.values[1].parse().unwrap();
            let new_idx = self.to_idx(q, r).unwrap();
            let new_mp = if reply.kind == "OK" {
                reply.values[2].parse().unwrap_or(0)
            } else {
                0
            };
            if let Some(u) = self.units.get_mut(&uid) {
                if new_idx != old_idx {
                    u.moved = true;
                }
                u.q = q;
                u.r = r;
                u.idx = new_idx;
                u.mp = new_mp;
            }
            if self.units.len() != prev_units {
                self.refresh_caches();
                return Some(true);
            } else if new_idx != old_idx {
                self.occ[old_idx] -= 1;
                self.occ[new_idx] += 1;
            }
        }
        Some(false)
    }

    fn do_attack(&mut self, uid: u32, tid: u32) -> Option<bool> {
        let cmd = format!("A {} {}", uid, tid);
        let reply = self.send_order(&cmd)?;
        if reply.ok {
            let target_hp: i32 = reply.values[0].parse().unwrap();
            let att_hp: i32 = reply.values[1].parse().unwrap();
            if let Some(t) = self.units.get_mut(&tid) {
                t.hp = target_hp;
            }
            if let Some(u) = self.units.get_mut(&uid) {
                u.hp = att_hp;
                u.acted = true;
                u.mp = 0;
            }
            if target_hp <= 0 {
                self.units.remove(&tid);
                self.seen_enemy_types.remove(&tid);
                self.ghosts.remove(&tid);
            }
            if att_hp <= 0 {
                self.units.remove(&uid);
            }
            self.refresh_caches();
            return Some(true);
        }
        Some(false)
    }

    fn do_capture(&mut self, uid: u32) -> Option<bool> {
        let cmd = format!("C {}", uid);
        let reply = self.send_order(&cmd)?;
        if reply.ok {
            let cell = self.units[&uid].idx;
            if let Some(u) = self.units.get_mut(&uid) {
                u.acted = true;
                u.mp = 0;
            }
            if let Some(cid) = self.city_at[cell] {
                if reply.values.first().map(|s| s.as_str()) == Some("captured") {
                    self.cities[cid].owner = self.player;
                    self.cities[cid].capturer = 0;
                    self.cities[cid].progress = 0;
                } else if let Some(p) = reply.values.first().and_then(|s| s.parse::<i32>().ok()) {
                    self.cities[cid].capturer = self.player;
                    self.cities[cid].progress = p;
                }
            }
            return Some(true);
        }
        Some(false)
    }

    fn do_build(&mut self, cid: usize, utype: usize) -> Option<bool> {
        let code = self.unit_types[utype].code.clone();
        let cmd = format!("B {} {}", cid, code);
        let reply = self.send_order(&cmd)?;
        if reply.ok {
            let new_id: u32 = reply.values[0].parse().unwrap();
            let c = &mut self.cities[cid];
            c.built = true;
            let q = c.q;
            let r = c.r;
            let idx = c.idx;
            let max_hp = self.unit_types[utype].max_hp;
            let cost = self.unit_types[utype].cost;
            self.gold -= cost;
            self.units.insert(
                new_id,
                Unit {
                    id: new_id,
                    owner: self.player,
                    utype,
                    q,
                    r,
                    idx,
                    hp: max_hp,
                    mp: 0,
                    acted: false,
                    moved: false,
                    fresh: true,
                },
            );
            self.my_unit_ids.push(new_id);
            self.occ[idx] += 1;
            return Some(true);
        }
        Some(false)
    }

    fn can_capture(&self, uid: u32) -> bool {
        if let Some(u) = self.units.get(&uid) {
            if u.owner != self.player || u.acted || u.fresh || !self.unit_types[u.utype].capture {
                return false;
            }
            if let Some(cid) = self.city_at[u.idx] {
                return self.cities[cid].owner != self.player;
            }
        }
        false
    }

    // Phase 0: Capture cities we're already standing on, saving full-HP units when fewer suffice.
    fn capture_in_place(&mut self) -> Option<()> {
        let mut by_city: HashMap<usize, Vec<(i32, u32)>> = HashMap::new();
        for &uid in &self.my_unit_ids {
            if self.can_capture(uid) {
                let u = &self.units[&uid];
                if let Some(cid) = self.city_at[u.idx] {
                    by_city.entry(cid).or_default().push((u.hp, uid));
                }
            }
        }
        for (cid, mut list) in by_city {
            let need = self.capture_points
                - if self.cities[cid].capturer == self.player {
                    self.cities[cid].progress
                } else {
                    0
                };
            list.sort_by_key(|x| x.0);
            if let Some(&(_, uid)) = list.iter().find(|&&(hp, _)| hp >= need) {
                self.do_capture(uid)?;
                continue;
            }
            list.sort_by_key(|x| Reverse(x.0));
            for (_, uid) in list {
                if self.cities[cid].owner == self.player {
                    break;
                }
                self.do_capture(uid)?;
            }
        }
        Some(())
    }

    // Phase 1: Rush foreign cities we can reach this turn.
    fn rush_cities(&mut self) -> Option<()> {
        let mut foreign: Vec<usize> = self
            .cities
            .iter()
            .filter(|c| c.owner != self.player && self.enemy_occ[c.idx] == 0)
            .map(|c| c.id)
            .collect();
        if foreign.is_empty() {
            return Some(());
        }
        foreign.sort_by_key(|&cid| {
            let c = &self.cities[cid];
            let owner_pri = if c.owner != 0 { 0 } else { 1 };
            (owner_pri, self.city_my_home_dist[cid])
        });

        let mut capturers: Vec<u32> = self
            .my_unit_ids
            .iter()
            .copied()
            .filter(|&uid| {
                let u = &self.units[&uid];
                !u.acted && !u.fresh && u.mp > 0 && self.unit_types[u.utype].capture
            })
            .collect();
        if capturers.is_empty() {
            return Some(());
        }

        let mut reaches: HashMap<u32, HashMap<usize, String>> = capturers
            .iter()
            .map(|&uid| (uid, self.reachable(uid, true)))
            .collect();
        let mut used: HashSet<u32> = HashSet::new();

        for cid in foreign {
            if self.out_of_time() {
                break;
            }
            let cidx = self.cities[cid].idx;
            if self.cities[cid].owner == self.player || self.enemy_occ[cidx] > 0 {
                continue;
            }
            let room = (self.stack_limit - self.occ[cidx]).max(0) as usize;
            if room == 0 {
                continue;
            }
            let mut group: Vec<u32> = capturers
                .iter()
                .copied()
                .filter(|uid| !used.contains(uid) && reaches[uid].contains_key(&cidx))
                .collect();
            if group.is_empty() {
                continue;
            }
            group.sort_by_key(|&uid| Reverse(self.units[&uid].hp));
            group.truncate(room);

            let progress = if self.cities[cid].capturer == self.player {
                self.cities[cid].progress
            } else {
                0
            };
            let need = self.capture_points - progress;
            let total_hp: i32 = group.iter().map(|&uid| self.units[&uid].hp).sum();
            let takes_now = total_hp >= need;

            if takes_now {
                // Use minimum units needed to flip it this turn!
                let mut trimmed = Vec::new();
                let mut acc = 0;
                for uid in group {
                    trimmed.push(uid);
                    acc += self.units[&uid].hp;
                    if acc >= need {
                        break;
                    }
                }
                group = trimmed;
            } else {
                let strength = self.support[cidx]
                    + group
                        .iter()
                        .map(|&uid| self.unit_worth(self.units[&uid].utype, self.units[&uid].hp))
                        .sum::<f64>();
                if self.threat[cidx] > 1.25 * strength {
                    continue;
                }
            }

            for uid in group {
                if self.cities[cid].owner == self.player || self.out_of_time() {
                    break;
                }
                used.insert(uid);
                let path = match reaches.get(&uid).and_then(|r| r.get(&cidx)) {
                    Some(p) => p.clone(),
                    None => continue,
                };
                if self.enemy_occ[cidx] > 0 {
                    break;
                }
                let spotted = self.do_move(uid, &path)?;
                if self.units.get(&uid).map(|u| u.idx) == Some(cidx) && self.can_capture(uid) {
                    self.do_capture(uid)?;
                }
                if spotted {
                    capturers.retain(|id| self.units.contains_key(id) && !used.contains(id));
                    reaches = capturers
                        .iter()
                        .map(|&id| (id, self.reachable(id, true)))
                        .collect();
                }
            }
        }
        Some(())
    }

    // Phase 2: Coordinated Focus-Fire Combat with Cost-Scaled Exposure & Finisher Selection!
    fn trade_score(
        &self,
        att_ut: usize,
        att_hp: i32,
        att_cell: usize,
        from_cell: usize,
        e: &Unit,
        e_hp_now: i32,
        is_part_of_kill: bool,
    ) -> (f64, i32, i32) {
        let ut = &self.unit_types[att_ut];
        let et = &self.unit_types[e.utype];
        let hit = self.damage(att_ut, att_hp, e.utype, e.idx);
        let new_e_hp = (e_hp_now - hit).max(0);
        let dealt = (e_hp_now - new_e_hp) as f64;
        let mut gain = (et.cost as f64) * dealt / (et.max_hp as f64);
        if new_e_hp == 0 {
            gain += (et.cost as f64) * 0.55;
        } else if is_part_of_kill {
            gain *= 1.30;
        }
        if let Some(cid) = self.city_at[e.idx] {
            let c = &self.cities[cid];
            if et.capture && c.owner != e.owner {
                gain += if new_e_hp == 0 { 160.0 } else { 55.0 };
            } else if c.owner == self.player && new_e_hp == 0 {
                gain += 120.0;
            } else if c.owner != self.player && new_e_hp == 0 {
                gain += 55.0;
            }
        }

        let mut loss = 0.0;
        let mut new_att_hp = att_hp;
        if new_e_hp > 0
            && et.in_range(self.cell_dist(from_cell, e.idx))
            && self.damage_table[e.utype][att_ut] > 0
        {
            let back = self.damage(e.utype, new_e_hp, att_ut, from_cell);
            new_att_hp = (att_hp - back).max(0);
            let taken = (att_hp - new_att_hp) as f64;
            loss = (ut.cost as f64) * taken / (ut.max_hp as f64);
            if new_att_hp == 0 {
                loss += (ut.cost as f64) * 0.55;
            }
        }

        // Be stricter on retaliation for non-kill trades (never suicide uphill into 40% hills!),
        // while staying aggressive on guaranteed kills.
        let eff_lw = if is_part_of_kill || new_e_hp == 0 {
            self.loss_weight
        } else {
            self.loss_weight.max(0.93)
        };
        let mut score = gain - eff_lw * loss;
        if from_cell != att_cell && self.expose_weight > 0.0 {
            let mut after = self.risk_at(from_cell, att_ut);
            if new_e_hp == 0 {
                let e_men = (self.first_strike_val(e.utype, e.hp, att_ut, 0) / self.par_trade)
                    * (1.0 - self.cell_defense[from_cell] as f64 / 100.0);
                after = (after - e_men).max(0.0);
            }
            let before = self.risk_at(att_cell, att_ut);
            // Nonlinear exposure risk: 2+ enemies threatening a hex means high probability of dying!
            let pen = |r: f64| -> f64 {
                let rc = r.min(3.5);
                let ex = (rc - 1.0).max(0.0);
                rc + 0.55 * ex * ex
            };
            let hp_scale = (new_att_hp as f64 / ut.max_hp as f64).max(0.35);
            let cost_scale = (ut.cost as f64 / 100.0).max(0.8) * hp_scale;
            score -= self.expose_weight * cost_scale * (pen(after) - pen(before));
        }
        (score, new_e_hp, new_att_hp)
    }

    fn fight(&mut self) -> Option<()> {
        if self.enemy_unit_ids.is_empty() {
            return Some(());
        }
        self.compute_threat_maps();

        loop {
            if self.out_of_time() || self.enemy_unit_ids.is_empty() {
                break;
            }

            let mut opts_by_enemy: HashMap<u32, Vec<(u32, usize, String, bool)>> = HashMap::new();
            let mut all_opts: Vec<(u32, u32, usize, String, bool)> = Vec::new();

            let ready: Vec<u32> = self
                .my_unit_ids
                .iter()
                .copied()
                .filter(|&uid| {
                    let u = &self.units[&uid];
                    !u.acted
                        && !u.fresh
                        && !self.failed_move.contains(&uid)
                        && !(self.unit_types[u.utype].no_move_attack && u.moved)
                })
                .collect();

            for uid in ready {
                let u = &self.units[&uid];
                let t = &self.unit_types[u.utype];
                let span = t.range_max + if t.no_move_attack { 0 } else { u.mp };
                let near_enemies: Vec<u32> = self
                    .enemy_unit_ids
                    .iter()
                    .copied()
                    .filter(|&eid| self.cell_dist(u.idx, self.units[&eid].idx) <= span)
                    .collect();
                if near_enemies.is_empty() {
                    continue;
                }
                let reach = if t.no_move_attack {
                    let mut m = HashMap::new();
                    m.insert(u.idx, String::new());
                    m
                } else {
                    self.reachable(uid, true)
                };

                for eid in near_enemies {
                    let e = &self.units[&eid];
                    let et = &self.unit_types[e.utype];
                    let mut best_cell: Option<(f64, i32, usize)> = None;
                    let q0 = self.cell_q[e.idx];
                    let r0 = self.cell_r[e.idx];
                    for dr in -t.range_max..=t.range_max {
                        for dq in (-t.range_max).max(-dr - t.range_max)
                            ..=t.range_max.min(-dr + t.range_max)
                        {
                            let d = (dq.abs() + dr.abs() + (dq + dr).abs()) / 2;
                            if d >= t.range_min && d <= t.range_max {
                                if let Some(h) = self.to_idx(q0 + dq, r0 + dr) {
                                    if let Some(dirs) = reach.get(&h) {
                                        if h != u.idx && self.occ[h] >= self.stack_limit {
                                            continue;
                                        }
                                        let no_ret = !et.in_range(d)
                                            || self.damage_table[e.utype][u.utype] == 0;
                                        let score = self.cell_defense[h] as f64
                                            - 28.0 * self.risk_at(h, u.utype)
                                            + if no_ret { 40.0 } else { 0.0 }
                                            + if self.cell_conceal[h] > 0 { 3.0 } else { 0.0 };
                                        let neg_len = -(dirs.len() as i32);
                                        if best_cell
                                            .map(|b| (score, neg_len) > (b.0, b.1))
                                            .unwrap_or(true)
                                        {
                                            best_cell = Some((score, neg_len, h));
                                        }
                                    }
                                }
                            }
                        }
                    }
                    if let Some((_, _, h)) = best_cell {
                        let d = self.cell_dist(h, e.idx);
                        let is_free =
                            !et.in_range(d) || self.damage_table[e.utype][u.utype] == 0;
                        let dirs = reach[&h].clone();
                        opts_by_enemy
                            .entry(eid)
                            .or_default()
                            .push((uid, h, dirs.clone(), is_free));
                        all_opts.push((uid, eid, h, dirs, is_free));
                    }
                }
            }

            if all_opts.is_empty() {
                break;
            }

            // 1. Search for best coordinated KILL PLAN
            let mut best_kill_plan: Option<(f64, u32, Vec<(u32, usize, String)>)> = None;
            for (&eid, attackers) in &opts_by_enemy {
                let e = &self.units[&eid];
                let mut free_atts: Vec<(u32, usize, String, i32)> = Vec::new();
                let mut melee_atts: Vec<(u32, usize, String, i32)> = Vec::new();
                for &(uid, h, ref dirs, is_free) in attackers {
                    let u = &self.units[&uid];
                    let dmg = self.damage(u.utype, u.hp, e.utype, e.idx);
                    if dmg <= 0 {
                        continue;
                    }
                    if is_free {
                        free_atts.push((uid, h, dirs.clone(), dmg));
                    } else {
                        melee_atts.push((uid, h, dirs.clone(), dmg));
                    }
                }
                let total_dmg: i32 = free_atts.iter().map(|x| x.3).sum::<i32>()
                    + melee_atts.iter().map(|x| x.3).sum::<i32>();
                if total_dmg < e.hp {
                    continue;
                }

                free_atts.sort_by_key(|x| Reverse(x.3));
                melee_atts.sort_by_key(|x| Reverse((x.3, self.cell_defense[x.1])));

                let mut plan: Vec<(u32, usize, String)> = Vec::new();
                let mut rem_hp = e.hp;
                let mut total_score = 0.0;
                let mut used_occ: HashMap<usize, i32> = HashMap::new();

                // Fire free attackers first
                for (uid, h, dirs, _) in &free_atts {
                    if rem_hp <= 0 {
                        break;
                    }
                    let u = &self.units[uid];
                    let (sc, new_hp, _) =
                        self.trade_score(u.utype, u.hp, u.idx, *h, e, rem_hp, true);
                    rem_hp = new_hp;
                    total_score += sc;
                    plan.push((*uid, *h, dirs.clone()));
                }

                if rem_hp > 0 {
                    // Single finisher: prefer LOWEST-HP unit that guarantees the kill (saves full-HP units!)
                    let mut best_single: Option<(f64, usize)> = None;
                    for (idx_m, &(uid, h, _, dmg)) in melee_atts.iter().enumerate() {
                        if dmg >= rem_hp {
                            let u = &self.units[&uid];
                            let (mut sc, _, _) =
                                self.trade_score(u.utype, u.hp, u.idx, h, e, rem_hp, true);
                            // Bonus for using a wounded unit as a 0-retaliation finisher!
                            sc += (100 - u.hp).max(0) as f64 * 0.25;
                            if best_single.map(|b| sc > b.0).unwrap_or(true) {
                                best_single = Some((sc, idx_m));
                            }
                        }
                    }
                    if let Some((sc, idx_m)) = best_single {
                        let (uid, h, dirs, _) = &melee_atts[idx_m];
                        total_score += sc;
                        rem_hp = 0;
                        plan.push((*uid, *h, dirs.clone()));
                    } else {
                        // Try best pair of melee attackers in BOTH orders (up to top 6 melee)
                        let top_m = melee_atts.len().min(6);
                        let mut best_pair: Option<(f64, usize, usize)> = None;
                        for i in 0..top_m {
                            for j in 0..top_m {
                                if i == j {
                                    continue;
                                }
                                let (u1, h1, _, d1) = &melee_atts[i];
                                let (u2, h2, _, d2) = &melee_atts[j];
                                if d1 + d2 < rem_hp {
                                    continue;
                                }
                                if h1 == h2
                                    && self.occ[*h1]
                                        + (if self.units[u1].idx == *h1 { 0 } else { 1 })
                                        + (if self.units[u2].idx == *h2 { 0 } else { 1 })
                                        > self.stack_limit
                                {
                                    continue;
                                }
                                let ua = &self.units[u1];
                                let ub = &self.units[u2];
                                let (sc1, hp1, _) = self.trade_score(
                                    ua.utype, ua.hp, ua.idx, *h1, e, rem_hp, true,
                                );
                                let (sc2, hp2, _) =
                                    self.trade_score(ub.utype, ub.hp, ub.idx, *h2, e, hp1, true);
                                if hp2 == 0 {
                                    let sc = sc1 + sc2;
                                    if best_pair.map(|b| sc > b.0).unwrap_or(true) {
                                        best_pair = Some((sc, i, j));
                                    }
                                }
                            }
                        }
                        if let Some((sc, i, j)) = best_pair {
                            total_score += sc;
                            rem_hp = 0;
                            plan.push((melee_atts[i].0, melee_atts[i].1, melee_atts[i].2.clone()));
                            plan.push((melee_atts[j].0, melee_atts[j].1, melee_atts[j].2.clone()));
                        } else {
                            for (uid, h, dirs, _) in &melee_atts {
                                if rem_hp <= 0 {
                                    break;
                                }
                                let u = &self.units[uid];
                                let extra = if u.idx == *h { 0 } else { 1 };
                                let cur_occ = self.occ[*h] + *used_occ.get(h).unwrap_or(&0);
                                if cur_occ + extra > self.stack_limit {
                                    continue;
                                }
                                *used_occ.entry(*h).or_insert(0) += extra;
                                let (sc, new_hp, _) =
                                    self.trade_score(u.utype, u.hp, u.idx, *h, e, rem_hp, true);
                                rem_hp = new_hp;
                                total_score += sc;
                                plan.push((*uid, *h, dirs.clone()));
                            }
                        }
                    }
                }

                if rem_hp == 0 && total_score > 0.0 {
                    // Prefer highest value per attacker
                    let rank = total_score / (plan.len() as f64).powf(0.35);
                    if best_kill_plan.as_ref().map(|b| rank > b.0).unwrap_or(true) {
                        best_kill_plan = Some((rank, eid, plan));
                    }
                }
            }

            if let Some((_, eid, plan)) = best_kill_plan {
                for (uid, h, dirs) in plan {
                    if self.out_of_time() || !self.units.contains_key(&eid) {
                        break;
                    }
                    if !self.units.contains_key(&uid) {
                        continue;
                    }
                    if !dirs.is_empty() {
                        self.do_move(uid, &dirs)?;
                        if self.units.get(&uid).map(|u| u.idx) != Some(h) {
                            self.failed_move.insert(uid);
                            break;
                        }
                    }
                    self.do_attack(uid, eid)?;
                }
                self.compute_threat_maps();
                continue;
            }

            // 2. Best single attack when no kill plan is profitable
            let mut best_single: Option<(f64, u32, u32, usize, String)> = None;
            for &(uid, eid, h, ref dirs, is_free) in &all_opts {
                let u = &self.units[&uid];
                let e = &self.units[&eid];
                if h != u.idx && self.occ[h] >= self.stack_limit {
                    continue;
                }
                let (mut sc, _, _) = self.trade_score(u.utype, u.hp, u.idx, h, e, e.hp, false);
                if is_free {
                    sc += 10.0;
                }
                if best_single.as_ref().map(|b| sc > b.0).unwrap_or(true) {
                    best_single = Some((sc, uid, eid, h, dirs.clone()));
                }
            }

            if let Some((sc, uid, eid, h, dirs)) = best_single {
                if sc <= 0.0 {
                    break;
                }
                if !dirs.is_empty() {
                    self.do_move(uid, &dirs)?;
                    if self.units.get(&uid).map(|u| u.idx) != Some(h) {
                        self.failed_move.insert(uid);
                        self.compute_threat_maps();
                        continue;
                    }
                }
                self.do_attack(uid, eid)?;
                self.compute_threat_maps();
            } else {
                break;
            }
        }
        Some(())
    }

    // Phase 3: Strategic Assignment, Smart Garrisoning, Tactical Positioning & Safe Scout Spotting
    fn advance(&mut self) -> Option<()> {
        self.refresh_caches();
        self.compute_threat_maps();

        let my_cities: Vec<usize> = self
            .cities
            .iter()
            .filter(|c| c.owner == self.player)
            .map(|c| c.id)
            .collect();

        // Pick a primary offensive target among enemy cities to concentrate our push!
        let primary_enemy_city: Option<usize> = self
            .cities
            .iter()
            .filter(|c| c.owner != 0 && c.owner != self.player)
            .min_by(|a, b| {
                let da = my_cities
                    .iter()
                    .map(|&mc| self.city_dist[a.id][self.cities[mc].idx])
                    .min()
                    .unwrap_or(INF_DIST) as f64
                    + self.threat[a.idx] / 120.0;
                let db = my_cities
                    .iter()
                    .map(|&mc| self.city_dist[b.id][self.cities[mc].idx])
                    .min()
                    .unwrap_or(INF_DIST) as f64
                    + self.threat[b.idx] / 120.0;
                da.partial_cmp(&db).unwrap()
            })
            .map(|c| c.id);

        // Goal cities: cid -> (value, strength_wanted, is_uncontested_neutral)
        let mut goals: HashMap<usize, (f64, f64, bool)> = HashMap::new();
        for c in &self.cities {
            let th = self.threat[c.idx];
            let strik = self.enemy_striker_cnt[c.idx];
            if c.owner == self.player {
                if th > 0.0 || strik > 0.0 || (c.capturer != 0 && c.capturer != self.player) {
                    let urg = if c.capturer != 0 && c.capturer != self.player {
                        2.2
                    } else {
                        1.2
                    };
                    goals.insert(c.id, (urg, 1.25 * th + 80.0, false));
                }
            } else if c.owner == 0 {
                let uncontested = th == 0.0 && strik == 0.0;
                goals.insert(
                    c.id,
                    (1.25, (self.capture_points as f64) + 1.2 * th, uncontested),
                );
            } else {
                // Focus fire our offensive wave on the primary enemy city!
                let is_primary = Some(c.id) == primary_enemy_city;
                let val = if is_primary { 0.55 } else { 0.20 };
                let want = if is_primary {
                    600.0 + 1.8 * th
                } else {
                    300.0 + 1.5 * th
                };
                goals.insert(c.id, (val, want, false));
            }
        }
        if goals.is_empty() {
            return Some(());
        }

        // Only garrison frontline/threatened cities (NOT deep rear cities!)
        let mut needs_garrison = vec![false; self.cities.len()];
        if self.round >= 8 {
            for &cid in &my_cities {
                let cidx = self.cities[cid].idx;
                let near_foreign = self
                    .cities
                    .iter()
                    .any(|o| o.owner != self.player && self.city_dist[cid][o.idx] <= 12);
                let near_enemy = self.threat[cidx] > 0.0
                    || self.enemy_striker_cnt[cidx] > 0.0
                    || self
                        .enemy_unit_ids
                        .iter()
                        .any(|&eid| self.cell_dist(cidx, self.units[&eid].idx) <= 8)
                    || self.ghosts.values().any(|g| self.cell_dist(cidx, g.idx) <= 8);
                if near_foreign || near_enemy {
                    needs_garrison[cid] = true;
                }
            }
        }

        let contested_goals: Vec<usize> = goals
            .keys()
            .copied()
            .filter(|&cid| self.cities[cid].owner != 0 || self.threat[self.cities[cid].idx] > 0.0)
            .collect();

        let mut plans: Vec<(f64, u32, Vec<(usize, f64)>)> = Vec::new();
        let mut scouts: Vec<u32> = Vec::new();
        let mut garrisoned: HashSet<usize> = HashSet::new();

        for &uid in &self.my_unit_ids {
            let u = &self.units[&uid];
            if u.acted || u.fresh || u.mp <= 0 {
                continue;
            }
            let t = &self.unit_types[u.utype];

            if let Some(cid) = self.city_at[u.idx] {
                if self.cities[cid].owner == self.player
                    && needs_garrison[cid]
                    && self.occ[u.idx] == 1
                    && !garrisoned.contains(&cid)
                    && !t.skirmish
                {
                    garrisoned.insert(cid);
                    continue;
                }
            }

            if t.skirmish && !t.capture {
                scouts.push(uid);
                continue;
            }

            // Healing rotation: only retreat to heal if a safe owned city is reasonably close!
            if !self.last_turn && (u.hp as f64) < (t.max_hp as f64) * 0.72 && !my_cities.is_empty()
            {
                let best_home = my_cities
                    .iter()
                    .copied()
                    .min_by(|&a, &b| {
                        let ca = self.city_dist[a][u.idx] as f64
                            + 4.0 * self.enemy_striker_cnt[self.cities[a].idx];
                        let cb = self.city_dist[b][u.idx] as f64
                            + 4.0 * self.enemy_striker_cnt[self.cities[b].idx];
                        ca.partial_cmp(&cb).unwrap()
                    })
                    .unwrap();
                if self.city_dist[best_home][u.idx] <= t.mv * 4 {
                    plans.push((-1.0, uid, vec![(best_home, 0.0)]));
                    continue;
                }
            }

            let candidate_goals: Vec<usize> = if t.capture || contested_goals.is_empty() {
                goals.keys().copied().collect()
            } else {
                contested_goals.clone()
            };

            let mut turns: Vec<(usize, f64)> = Vec::new();
            let mut min_t = f64::INFINITY;
            for cid in candidate_goals {
                let d = self.city_dist[cid][u.idx];
                if d < INF_DIST {
                    let tr = d as f64 / t.mv.max(1) as f64;
                    if tr < min_t {
                        min_t = tr;
                    }
                    turns.push((cid, tr));
                }
            }
            if !turns.is_empty() {
                plans.push((min_t, uid, turns));
            }
        }

        plans.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap().then(a.1.cmp(&b.1)));

        let mut load: HashMap<usize, f64> = goals.keys().map(|&c| (c, 0.0)).collect();
        let mut neutral_cap_hp: HashMap<usize, i32> = HashMap::new();
        // Account for friendly capturers ALREADY standing on neutral cities!
        for &uid in &self.my_unit_ids {
            let u = &self.units[&uid];
            if self.unit_types[u.utype].capture {
                if let Some(cid) = self.city_at[u.idx] {
                    if self.cities[cid].owner == 0 && (u.acted || u.fresh) {
                        *neutral_cap_hp.entry(cid).or_insert(0) += u.hp;
                    }
                }
            }
        }

        for (_, uid, turns) in plans {
            if self.out_of_time() {
                break;
            }
            let u = &self.units[&uid];
            let chosen_cid = if turns.len() == 1 {
                turns[0].0
            } else {
                let mut best_c = turns[0].0;
                let mut best_sc = f64::NEG_INFINITY;
                for &(cid, tr) in &turns {
                    let (val, want, uncontested_neutral) = goals[&cid];
                    let eff_val = val;
                    let ld = *load.get(&cid).unwrap_or(&0.0);
                    let sc = if uncontested_neutral && self.unit_types[u.utype].capture {
                        let need = self.capture_points
                            - if self.cities[cid].capturer == self.player {
                                self.cities[cid].progress
                            } else {
                                0
                            };
                        let assigned = *neutral_cap_hp.get(&cid).unwrap_or(&0);
                        if assigned >= need {
                            // Already have enough capturers for 1-turn capture -> go elsewhere!
                            (eff_val * 0.02) / (tr + 2.0) / (1.0 + ld / want.max(1.0))
                        } else {
                            // Pair up capturers for 1-turn capture! No load penalty until need is met!
                            eff_val / (tr + 1.5)
                        }
                    } else {
                        eff_val / (tr + 2.0) / (1.0 + ld / want.max(1.0))
                    };
                    if sc > best_sc {
                        best_sc = sc;
                        best_c = cid;
                    }
                }
                best_c
            };

            if let Some(l) = load.get_mut(&chosen_cid) {
                *l += self.unit_worth(u.utype, u.hp);
            }
            if self.unit_types[u.utype].capture && self.cities[chosen_cid].owner == 0 {
                *neutral_cap_hp.entry(chosen_cid).or_insert(0) += u.hp;
            }
            self.debug_line(u.idx, self.cities[chosen_cid].idx, "orange");
            self.march_unit(uid, chosen_cid)?;
        }

        self.post_scouts(scouts, true)?;
        let msg = format!(
            "R{} g={} c={} u={}",
            self.round,
            self.gold,
            my_cities.len(),
            self.my_unit_ids.len()
        );
        self.debug_text(&msg);
        Some(())
    }

    fn march_unit(&mut self, uid: u32, cid: usize) -> Option<()> {
        let u = match self.units.get(&uid) {
            Some(u) => u.clone(),
            None => return Some(()),
        };
        let t = self.unit_types[u.utype].clone();
        let hurt = u.hp < t.max_hp;
        let reach = self.reachable(uid, true);

        let mut best: Option<(f64, i32, String)> = None;
        for (&h, dirs) in &reach {
            let d = self.city_dist[cid][h];
            if d >= INF_DIST {
                continue;
            }
            let risk = self.risk_at(h, u.utype);
            let near_danger = risk > 0.0 || self.threat[h] > 0.0;
            // Pure speed when safely marching in rear; tactical terrain when near enemies!
            let terrain_bonus = if near_danger {
                (self.cell_defense[h] as f64) / 34.0
                    + if self.cell_conceal[h] > 0 && !self.enemy_zoc[h] {
                        0.45
                    } else {
                        0.0
                    }
            } else {
                (self.cell_defense[h] as f64) / 400.0
                    + if self.cell_conceal[h] > 0 { 0.05 } else { 0.0 }
            };
            // Push more confidently onto defensive terrain when we have clear local superiority!
            let eff_risk_w = if self.support[h] > 1.7 * self.threat[h] && self.cell_defense[h] >= 25
            {
                self.risk_weight * 0.72
            } else {
                self.risk_weight
            };
            let mut score = -(d as f64) - eff_risk_w * risk.min(6.0) + terrain_bonus;

            if let Some(here_cid) = self.city_at[h] {
                if self.cities[here_cid].owner == self.player {
                    if hurt {
                        score += 6.5;
                    }
                    let others = self.occ[h] - if h == u.idx { 1 } else { 0 };
                    if others >= self.stack_limit - 1 {
                        score -= 11.0;
                    }
                }
            }

            if t.no_move_attack {
                let in_range_enemy = self
                    .enemy_unit_ids
                    .iter()
                    .any(|&eid| t.in_range(self.cell_dist(h, self.units[&eid].idx)));
                if in_range_enemy {
                    score += 8.0;
                }
            }

            let neg_len = -(dirs.len() as i32);
            if best
                .as_ref()
                .map(|b| (score, neg_len) > (b.0, b.1))
                .unwrap_or(true)
            {
                best = Some((score, neg_len, dirs.clone()));
            }
        }

        if let Some((_, _, dirs)) = best {
            if !dirs.is_empty() {
                self.do_move(uid, &dirs)?;
            }
        }
        if self.can_capture(uid) {
            self.do_capture(uid)?;
        }
        Some(())
    }

    // Post scouts safely to uncover fog around the front line, or raid undefended enemy cities
    fn post_scouts(&mut self, scouts: Vec<u32>, allow_raid: bool) -> Option<()> {
        if scouts.is_empty() {
            return Some(());
        }
        let mut known: Vec<usize> = self
            .enemy_unit_ids
            .iter()
            .map(|&eid| self.units[&eid].idx)
            .collect();
        for g in self.ghosts.values() {
            known.push(g.idx);
        }
        for c in &self.cities {
            if c.owner != 0 && c.owner != self.player {
                known.push(c.idx);
            }
        }

        let army_vis = self.compute_visibility(false);
        let mut want = vec![false; self.ncells];
        for &uid in &self.my_unit_ids {
            let u = &self.units[&uid];
            let t = &self.unit_types[u.utype];
            if t.skirmish && !t.capture {
                continue;
            }
            if known.iter().any(|&k| self.cell_dist(u.idx, k) <= 8) {
                let q0 = self.cell_q[u.idx];
                let r0 = self.cell_r[u.idx];
                for dr in -5..=5 {
                    for dq in (-5i32).max(-dr - 5)..=5i32.min(-dr + 5) {
                        if let Some(c) = self.to_idx(q0 + dq, r0 + dr) {
                            if self.cell_passable[c] && !army_vis[c] {
                                want[c] = true;
                            }
                        }
                    }
                }
            }
        }

        for sid in scouts {
            if self.out_of_time() {
                break;
            }
            let u = match self.units.get(&sid) {
                Some(u) => u.clone(),
                None => continue,
            };
            let base_v = self.unit_types[u.utype].vision;
            let reach = self.reachable(sid, true);
            let mut best: Option<(f64, i32, usize, String, i32)> = None;
            for (&h, dirs) in &reach {
                let v = base_v + self.cell_vbonus[h];
                let q0 = self.cell_q[h];
                let r0 = self.cell_r[h];
                let mut gain = 0;
                for dr in -v..=v {
                    for dq in (-v).max(-dr - v)..=v.min(-dr + v) {
                        if let Some(c) = self.to_idx(q0 + dq, r0 + dr) {
                            if want[c] {
                                let d = (dq.abs() + dr.abs() + (dq + dr).abs()) / 2;
                                if d <= 1 || d + self.cell_conceal[c] <= v {
                                    gain += 1;
                                }
                            }
                        }
                    }
                }
                let sc = gain as f64 - 30.0 * self.risk_at(h, u.utype);
                let neg_len = -(dirs.len() as i32);
                if best
                    .as_ref()
                    .map(|b| (sc, neg_len) > (b.0, b.1))
                    .unwrap_or(true)
                {
                    best = Some((sc, neg_len, h, dirs.clone(), gain));
                }
            }
            if let Some((_, _, h, dirs, gain)) = best {
                if gain > 0 {
                    if !dirs.is_empty() {
                        self.do_move(sid, &dirs)?;
                    }
                    let v = base_v + self.cell_vbonus[h];
                    let q0 = self.cell_q[h];
                    let r0 = self.cell_r[h];
                    for dr in -v..=v {
                        for dq in (-v).max(-dr - v)..=v.min(-dr + v) {
                            if let Some(c) = self.to_idx(q0 + dq, r0 + dr) {
                                let d = (dq.abs() + dr.abs() + (dq + dr).abs()) / 2;
                                if d <= 1 || d + self.cell_conceal[c] <= v {
                                    want[c] = false;
                                }
                            }
                        }
                    }
                    continue;
                }
            }
            // Otherwise raid an undefended enemy city (only in post-combat phase)
            if allow_raid {
                self.raid_scout(sid)?;
            }
        }
        Some(())
    }

    fn raid_scout(&mut self, sid: u32) -> Option<()> {
        let u = match self.units.get(&sid) {
            Some(u) => u.clone(),
            None => return Some(()),
        };
        let enemy_cities: Vec<usize> = self
            .cities
            .iter()
            .filter(|c| c.owner != 0 && c.owner != self.player)
            .map(|c| c.id)
            .collect();
        let reach = self.reachable(sid, self.stack_limit == 1);
        let mut best: Option<(f64, i32, String)> = None;
        for &cid in &enemy_cities {
            let cidx = self.cities[cid].idx;
            let others = self.occ[cidx] - if u.idx == cidx { 1 } else { 0 };
            if others > 0 {
                continue;
            }
            for (&h, dirs) in &reach {
                let d = self.city_dist[cid][h];
                if d >= INF_DIST {
                    continue;
                }
                let is_other_foreign = self
                    .city_at[h]
                    .map(|o| self.cities[o].owner != self.player && o != cid)
                    .unwrap_or(false);
                if is_other_foreign {
                    continue;
                }
                let sc = -(d as f64)
                    - 25.0 * self.risk_at(h, u.utype)
                    + if h == cidx { 14.0 } else { 0.0 };
                let neg_len = -(dirs.len() as i32);
                if best
                    .as_ref()
                    .map(|b| (sc, neg_len) > (b.0, b.1))
                    .unwrap_or(true)
                {
                    best = Some((sc, neg_len, dirs.clone()));
                }
            }
        }
        if let Some((_, _, dirs)) = best {
            if !dirs.is_empty() {
                self.do_move(sid, &dirs)?;
            }
        }
        Some(())
    }

    // Phase 4: Mathematically Optimal Counter-Building (Zero Waste, Rule-Variant Adaptive!)
    fn build_all(&mut self) -> Option<()> {
        let nt = self.unit_types.len();
        if nt == 0 {
            return Some(());
        }
        self.refresh_caches();

        let foreign_cities: Vec<usize> = self
            .cities
            .iter()
            .filter(|c| c.owner != self.player)
            .map(|c| c.id)
            .collect();

        let mut sites: Vec<usize> = self
            .cities
            .iter()
            .filter(|c| {
                c.owner == self.player
                    && !c.built
                    && self.enemy_occ[c.idx] == 0
                    && self.occ[c.idx] < self.stack_limit
            })
            .map(|c| c.id)
            .collect();

        sites.sort_by(|&a, &b| {
            let ca = &self.cities[a];
            let cb = &self.cities[b];
            let ta = self.threat[ca.idx];
            let tb = self.threat[cb.idx];
            let da = foreign_cities
                .iter()
                .map(|&g| self.city_dist[g][ca.idx])
                .min()
                .unwrap_or(0);
            let db = foreign_cities
                .iter()
                .map(|&g| self.city_dist[g][cb.idx])
                .min()
                .unwrap_or(0);
            tb.partial_cmp(&ta).unwrap().then(da.cmp(&db)).then(a.cmp(&b))
        });

        if sites.is_empty() {
            return Some(());
        }

        // Last turn: exact knapsack for maximum army value
        if self.last_turn {
            let mut dp: HashMap<i32, Vec<usize>> = HashMap::new();
            dp.insert(0, Vec::new());
            for _ in 0..sites.len() {
                let snap: Vec<(i32, Vec<usize>)> =
                    dp.iter().map(|(&k, v)| (k, v.clone())).collect();
                for (tot, picks) in snap {
                    if picks.len() < sites.len() {
                        for ut in 0..nt {
                            let c = self.unit_types[ut].cost;
                            if tot + c <= self.gold && !dp.contains_key(&(tot + c)) {
                                let mut np = picks.clone();
                                np.push(ut);
                                dp.insert(tot + c, np);
                            }
                        }
                    }
                }
            }
            if let Some((_, best_picks)) = dp.into_iter().max_by_key(|x| x.0) {
                for (cid, ut) in sites.into_iter().zip(best_picks) {
                    self.do_build(cid, ut)?;
                }
            }
            return Some(());
        }

        // Compute enemy mix from seen alive enemies + prior on cheapest capturer
        let mut mix = vec![0.0_f64; nt];
        for &ut in self.seen_enemy_types.values() {
            mix[ut] += 1.0;
        }
        let cheapest_cap = (0..nt)
            .filter(|&i| self.unit_types[i].capture)
            .min_by_key(|&i| self.unit_types[i].cost);
        if let Some(cc) = cheapest_cap {
            mix[cc] += 2.0;
        } else {
            mix[0] += 2.0;
        }

        let my_city_cnt = self.cities.iter().filter(|c| c.owner == self.player).count() as f64;
        let upkeep_ratio = (self.my_unit_ids.len() as f64 * self.unit_upkeep as f64)
            / (my_city_cnt * self.city_income as f64).max(1.0);

        let mut scores = vec![0.0_f64; nt];
        for ut in 0..nt {
            let t = &self.unit_types[ut];
            let mut dealt = 0.0;
            let mut taken = 0.0;
            for e in 0..nt {
                if mix[e] <= 0.0 {
                    continue;
                }
                let et = &self.unit_types[e];
                dealt += mix[e] * (self.damage_table[ut][e] as f64) * (et.cost as f64)
                    / (et.max_hp as f64);
                taken += mix[e] * (self.damage_table[e][ut] as f64);
            }
            taken = taken.max(1.0);
            let mut sc = (dealt * t.max_hp as f64) / (taken * t.cost.max(1) as f64);
            if t.range_min > 1 {
                sc *= 1.45;
            }
            // Only boost higher-cost units when upkeep is actually capping our economy!
            if upkeep_ratio > 0.72 {
                sc *= 1.0 + (upkeep_ratio - 0.72) * (t.cost as f64 / 120.0);
            }
            scores[ut] = sc;
        }

        let top = scores.iter().copied().fold(0.0_f64, f64::max).max(1e-6);
        let shares: Vec<f64> = scores.iter().map(|&s| (s / top).powi(8)).collect();

        let mut counts = vec![0i32; nt];
        for &uid in &self.my_unit_ids {
            counts[self.units[&uid].utype] += 1;
        }

        let neutral_left = self.cities.iter().any(|c| c.owner == 0);

        for cid in sites {
            if self.out_of_time() {
                break;
            }
            let total: i32 = counts.iter().sum();
            let cap_cnt: i32 = (0..nt)
                .filter(|&u| self.unit_types[u].capture)
                .map(|u| counts[u])
                .sum();

            // Eligible types: affordable AND within top tier (share >= 0.10) — NEVER build junk scouts!
            let mut options: Vec<usize> = (0..nt)
                .filter(|&u| self.unit_types[u].cost <= self.gold && shares[u] >= 0.10)
                .collect();

            // Early game or while neutral cities remain: prioritize capturers heavily
            if total < 8 || cap_cnt * 2 < total + 1 || (neutral_left && cap_cnt * 3 < total * 2 + 1)
            {
                let cap_opts: Vec<usize> = options
                    .iter()
                    .copied()
                    .filter(|&u| self.unit_types[u].capture)
                    .collect();
                if !cap_opts.is_empty() {
                    options = cap_opts;
                }
            }

            if options.is_empty() {
                break;
            }

            let pick = *options
                .iter()
                .max_by(|&&a, &&b| {
                    let ka = (shares[a] / (counts[a] as f64 + 1.0), shares[a]);
                    let kb = (shares[b] / (counts[b] as f64 + 1.0), shares[b]);
                    ka.partial_cmp(&kb).unwrap()
                })
                .unwrap();

            if self.do_build(cid, pick)? {
                counts[pick] += 1;
            }
        }
        Some(())
    }

    fn parse_turn(&mut self, first_line: &str) -> bool {
        self.turn_start = Instant::now();
        self.debug_count = 0;
        let parts: Vec<&str> = first_line.split_whitespace().collect();
        self.round = parts
            .get(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or(self.round + 1);
        self.cities = self
            .init_cities
            .iter()
            .map(|&(id, q, r, owner)| City {
                id,
                q,
                r,
                idx: self.to_idx(q, r).unwrap(),
                owner,
                capturer: 0,
                progress: 0,
                built: false,
            })
            .collect();
        self.units.clear();

        let mut hit_logs: Vec<(u32, usize, usize, i32)> = Vec::new();

        loop {
            let line = match self.read_line() {
                Some(l) => l,
                None => return false,
            };
            let t: Vec<&str> = line.split_whitespace().collect();
            if t.is_empty() {
                continue;
            }
            match t[0] {
                "GO" => break,
                "TIME" => {
                    self.time_ms = t[1].parse().unwrap_or(5000);
                }
                "GOLD" => {
                    self.gold = t[1].parse().unwrap_or(0);
                }
                "CITY" => {
                    let cid: usize = t[1].parse().unwrap();
                    self.cities[cid].owner = t[2].parse().unwrap();
                    self.cities[cid].capturer = t[3].parse().unwrap();
                    self.cities[cid].progress = t[4].parse().unwrap();
                }
                "UNIT" => {
                    let id: u32 = t[1].parse().unwrap();
                    let owner: i32 = t[2].parse().unwrap();
                    let utype = self.type_by_code[t[3]];
                    let q: i32 = t[4].parse().unwrap();
                    let r: i32 = t[5].parse().unwrap();
                    let hp: i32 = t[6].parse().unwrap();
                    let idx = self.to_idx(q, r).unwrap();
                    let mp = if owner == self.player {
                        self.unit_types[utype].mv
                    } else {
                        0
                    };
                    self.units.insert(
                        id,
                        Unit {
                            id,
                            owner,
                            utype,
                            q,
                            r,
                            idx,
                            hp,
                            mp,
                            acted: false,
                            moved: false,
                            fresh: false,
                        },
                    );
                    if owner != self.player {
                        self.seen_enemy_types.insert(id, utype);
                    }
                }
                "TILE" => {
                    let q: i32 = t[1].parse().unwrap();
                    let r: i32 = t[2].parse().unwrap();
                    let ch = t[3].chars().next().unwrap();
                    if let (Some(idx), Some(ter)) = (self.to_idx(q, r), self.terrains.get(&ch)) {
                        self.cell_passable[idx] = ter.move_cost > 0;
                        self.cell_move_cost[idx] = ter.move_cost;
                        self.cell_defense[idx] = ter.defense;
                        self.cell_vbonus[idx] = ter.vision_bonus;
                        self.cell_conceal[idx] = ter.conceal;
                    }
                }
                "LOG" => {
                    if t.len() >= 9 && t[1] == "HIT" {
                        let att_id: u32 = t[2].parse().unwrap_or(0);
                        if let Some(&utype) = self.type_by_code.get(t[3]) {
                            let q: i32 = t[4].parse().unwrap_or(0);
                            let r: i32 = t[5].parse().unwrap_or(0);
                            let att_hp: i32 = t[8].parse().unwrap_or(0);
                            if let Some(idx) = self.to_idx(q, r) {
                                hit_logs.push((att_id, utype, idx, att_hp));
                            }
                        }
                    }
                }
                "GAMEOVER" => return false,
                _ => {}
            }
        }

        for (att_id, utype, idx, att_hp) in hit_logs {
            if att_hp > 0 {
                self.seen_enemy_types.insert(att_id, utype);
                self.ghosts.insert(
                    att_id,
                    Ghost {
                        id: att_id,
                        utype,
                        idx,
                        hp: att_hp,
                        seen_round: self.round - 1,
                    },
                );
            } else {
                self.seen_enemy_types.remove(&att_id);
                self.ghosts.remove(&att_id);
            }
        }

        true
    }

    fn on_turn(&mut self) -> Option<()> {
        self.failed_move.clear();
        self.last_turn = self.round >= self.round_limit;
        if self.last_turn && self.player == 2 {
            self.loss_weight = 1.0;
            self.expose_weight = 0.0;
            self.risk_weight = 0.0;
        } else if self.last_turn {
            self.loss_weight = 1.0;
            self.expose_weight = 40.0;
            self.risk_weight = 48.0;
        } else {
            self.loss_weight = 0.80;
            self.expose_weight = 12.0;
            self.risk_weight = 12.0;
        }

        self.refresh_caches();
        let vis = self.compute_visibility(true);
        let round = self.round;
        let units_ref = &self.units;
        self.ghosts.retain(|id, g| {
            !units_ref.contains_key(id) && (round - g.seen_round) < 8 && !vis[g.idx]
        });

        self.capture_in_place()?;
        // Safe recon BEFORE combat so our army sees hidden enemies in forests/fog first!
        self.compute_threat_maps();
        let pre_scouts: Vec<u32> = self
            .my_unit_ids
            .iter()
            .copied()
            .filter(|&uid| {
                let u = &self.units[&uid];
                let t = &self.unit_types[u.utype];
                !u.acted && !u.fresh && u.mp > 0 && t.skirmish && !t.capture
            })
            .collect();
        self.post_scouts(pre_scouts, false)?;
        self.rush_cities()?;
        self.fight()?;
        self.refresh_caches();
        self.rush_cities()?;
        self.advance()?;

        for &eid in &self.enemy_unit_ids {
            let e = &self.units[&eid];
            self.ghosts.insert(
                e.id,
                Ghost {
                    id: e.id,
                    utype: e.utype,
                    idx: e.idx,
                    hp: e.hp,
                    seen_round: self.round,
                },
            );
        }

        self.build_all()?;
        Some(())
    }

    fn run(&mut self) {
        if !self.parse_init() {
            return;
        }
        loop {
            let line = match self.read_line() {
                Some(l) => l,
                None => break,
            };
            let kw = match line.split_whitespace().next() {
                Some(k) => k,
                None => continue,
            };
            if kw == "TURN" {
                if !self.parse_turn(&line) {
                    break;
                }
                if self.on_turn().is_none() {
                    break;
                }
                let end_cmd = format!("END {}", self.round);
                self.send_line(&end_cmd);
            } else if kw == "GAMEOVER" {
                break;
            }
        }
    }
}

fn main() {
    let mut bot = Bot::new();
    bot.run();
}
