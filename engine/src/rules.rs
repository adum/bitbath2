//! Rule tables and constants (SPEC §3.2, §3.3, §3.10, §7). Every value here
//! is sent to bots in the init message and can be overridden per match.

/// A terrain type. A `move_cost` of 0 means impassable.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Terrain {
    pub ch: char,
    pub name: String,
    pub move_cost: u32,
    /// Percent of damage blocked for a unit standing here.
    pub defense: u32,
    /// Added to the vision of a unit standing here.
    pub vision_bonus: u32,
    /// Hides this hex from observers: it's seen from at most `vision −
    /// conceal` hexes away, though always from next door (SPEC §4).
    pub conceal: u32,
}

impl Terrain {
    pub fn passable(&self) -> bool {
        self.move_cost > 0
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnitType {
    pub code: char,
    pub name: String,
    pub cost: u32,
    pub max_hp: u32,
    pub mv: u32,
    pub vision: u32,
    pub range_min: u32,
    pub range_max: u32,
    pub capture: bool,
    pub zoc: bool,
    pub skirmish: bool,
    pub no_move_attack: bool,
}

impl UnitType {
    pub fn in_range(&self, distance: u32) -> bool {
        self.range_min <= distance && distance <= self.range_max
    }

    /// Flags as sent in `UNITTYPE` lines: comma-separated, or `-` for none.
    pub fn flags(&self) -> String {
        let mut f = Vec::new();
        if self.capture {
            f.push("capture");
        }
        if self.zoc {
            f.push("zoc");
        }
        if self.skirmish {
            f.push("skirmish");
        }
        if self.no_move_attack {
            f.push("no_move_attack");
        }
        if f.is_empty() {
            "-".into()
        } else {
            f.join(",")
        }
    }

    pub fn set_flags(&mut self, flags: &str) -> Result<(), String> {
        self.capture = false;
        self.zoc = false;
        self.skirmish = false;
        self.no_move_attack = false;
        for f in flags.split(',').map(str::trim).filter(|f| !f.is_empty() && *f != "-") {
            match f {
                "capture" => self.capture = true,
                "zoc" => self.zoc = true,
                "skirmish" => self.skirmish = true,
                "no_move_attack" => self.no_move_attack = true,
                _ => return Err(format!("unknown unit flag '{f}'")),
            }
        }
        Ok(())
    }
}

macro_rules! constants {
    ($($name:ident = $default:expr,)*) => {
        /// Numeric rule constants, sent as `RULE <name> <value>` lines.
        #[derive(Clone, Debug, PartialEq, Eq)]
        pub struct Constants {
            $(pub $name: u32,)*
        }

        impl Default for Constants {
            fn default() -> Constants {
                Constants { $($name: $default,)* }
            }
        }

        impl Constants {
            /// Constant names, in `RULE` line order.
            pub const NAMES: &'static [&'static str] = &[$(stringify!($name),)*];

            pub fn get(&self, name: &str) -> Option<u32> {
                match name {
                    $(stringify!($name) => Some(self.$name),)*
                    _ => None,
                }
            }

            /// Sets a constant. Returns false if there is no such constant.
            pub fn set(&mut self, name: &str, value: u32) -> bool {
                match name {
                    $(stringify!($name) => self.$name = value,)*
                    _ => return false,
                }
                true
            }
        }
    };
}

constants! {
    map_width = 40,
    map_height = 30,
    neutral_cities = 10,
    stack_limit = 4,
    round_limit = 200,
    start_gold = 300,
    city_income = 100,
    unit_upkeep = 5,
    city_heal = 20,
    city_vision = 2,
    capture_points = 200,
    time_bank_ms = 5000,
    time_increment_ms = 1000,
    time_keep_pct = 50,
    time_bank_cap_ms = 10000,
    init_ms = 10000,
    hang_turns = 3,
    max_orders_per_turn = 20000,
    max_line_bytes = 1024,
}

/// The terrain character that marks city hexes.
pub const CITY_CHAR: char = 'C';

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rules {
    pub terrains: Vec<Terrain>,
    pub units: Vec<UnitType>,
    /// Base damage, indexed `[attacker][defender]` in `units` order.
    pub damage: Vec<Vec<u32>>,
    pub c: Constants,
    /// Unit codes each player starts with on their home city.
    pub start_units: Vec<char>,
}

impl Default for Rules {
    fn default() -> Rules {
        let terrain = |ch, name: &str, move_cost, defense, vision_bonus, conceal| Terrain {
            ch,
            name: name.into(),
            move_cost,
            defense,
            vision_bonus,
            conceal,
        };
        let unit = |code, name: &str, cost, mv, vision, range_min, range_max, flags: &str| {
            let mut u = UnitType {
                code,
                name: name.into(),
                cost,
                max_hp: 100,
                mv,
                vision,
                range_min,
                range_max,
                capture: false,
                zoc: false,
                skirmish: false,
                no_move_attack: false,
            };
            u.set_flags(flags).expect("default flags are valid");
            u
        };
        Rules {
            terrains: vec![
                terrain('.', "plains", 1, 0, 0, 0),
                terrain('f', "forest", 2, 25, 0, 1),
                terrain('h', "hills", 2, 40, 1, 0),
                terrain(CITY_CHAR, "city", 1, 30, 0, 0),
                terrain('^', "mountains", 0, 0, 0, 0),
                terrain('~', "water", 0, 0, 0, 0),
            ],
            units: vec![
                unit('I', "infantry", 100, 3, 2, 1, 1, "capture,zoc"),
                unit('P', "pikeman", 150, 3, 2, 1, 1, "capture,zoc"),
                unit('K', "cavalry", 225, 5, 3, 1, 1, "zoc"),
                unit('A', "archer", 200, 3, 2, 2, 2, "zoc"),
                unit('S', "scout", 80, 6, 4, 1, 1, "skirmish"),
            ],
            damage: vec![
                vec![55, 45, 40, 65, 75],
                vec![45, 50, 80, 55, 70],
                vec![70, 35, 55, 85, 90],
                vec![50, 55, 40, 45, 70],
                vec![20, 15, 10, 25, 40],
            ],
            c: Constants::default(),
            start_units: vec!['I', 'I', 'S'],
        }
    }
}

fn single_char(s: &str) -> Option<char> {
    let mut chars = s.chars();
    match (chars.next(), chars.next()) {
        (Some(c), None) => Some(c),
        _ => None,
    }
}

impl Rules {
    pub fn terrain_index(&self, ch: char) -> Option<usize> {
        self.terrains.iter().position(|t| t.ch == ch)
    }

    pub fn unit_index(&self, code: char) -> Option<usize> {
        self.units.iter().position(|u| u.code == code)
    }

    /// Finds a unit type by code (`I`) or name (`infantry`).
    fn unit_by_key(&self, key: &str) -> Result<usize, String> {
        self.units
            .iter()
            .position(|u| u.name == key || single_char(key) == Some(u.code))
            .ok_or_else(|| format!("no unit type '{key}'"))
    }

    /// Overrides one rule from a config key, such as `round_limit`,
    /// `start_units`, `unit.I.cost`, `damage.K.P`, or `terrain.hills.defense`.
    pub fn set(&mut self, key: &str, value: &str) -> Result<(), String> {
        let num = || {
            value
                .parse::<u32>()
                .map_err(|_| format!("{key}: '{value}' is not a non-negative integer"))
        };
        if self.c.get(key).is_some() {
            self.c.set(key, num()?);
            return Ok(());
        }
        if key == "start_units" {
            let mut codes = Vec::new();
            for s in value.split(',').map(str::trim).filter(|s| !s.is_empty()) {
                codes.push(single_char(s).ok_or_else(|| format!("start_units: '{s}' is not a unit code"))?);
            }
            self.start_units = codes;
            return Ok(());
        }
        let parts: Vec<&str> = key.split('.').collect();
        match parts.as_slice() {
            ["unit", name, field] => {
                let i = self.unit_by_key(name)?;
                let u = &mut self.units[i];
                match *field {
                    "cost" => u.cost = num()?,
                    "max_hp" => u.max_hp = num()?,
                    "move" => u.mv = num()?,
                    "vision" => u.vision = num()?,
                    "range_min" => u.range_min = num()?,
                    "range_max" => u.range_max = num()?,
                    "flags" => u.set_flags(value)?,
                    _ => return Err(format!("unknown unit field '{field}'")),
                }
            }
            ["damage", att, def] => {
                let a = self.unit_by_key(att)?;
                let d = self.unit_by_key(def)?;
                self.damage[a][d] = num()?;
            }
            ["terrain", name, field] => {
                let t = self
                    .terrains
                    .iter_mut()
                    .find(|t| t.name == *name)
                    .ok_or_else(|| format!("no terrain '{name}'"))?;
                match *field {
                    "move_cost" => t.move_cost = num()?,
                    "defense" => t.defense = num()?,
                    "vision_bonus" => t.vision_bonus = num()?,
                    "conceal" => t.conceal = num()?,
                    _ => return Err(format!("unknown terrain field '{field}'")),
                }
            }
            _ => return Err(format!("unknown rule '{key}'")),
        }
        Ok(())
    }

    /// Checks that the rules are internally consistent.
    pub fn validate(&self) -> Result<(), String> {
        let c = &self.c;
        if c.map_height < 2 || !c.map_height.is_multiple_of(2) {
            return Err("map_height must be even".into());
        }
        if c.stack_limit == 0 || c.capture_points == 0 || c.round_limit == 0 {
            return Err("stack_limit, capture_points, and round_limit must be positive".into());
        }
        if c.time_keep_pct > 100 {
            return Err("time_keep_pct must be at most 100".into());
        }
        if c.max_line_bytes < 64 {
            return Err("max_line_bytes must be at least 64".into());
        }
        let ok_token = |s: &str| !s.is_empty() && s.chars().all(|ch| ch.is_ascii_graphic());
        for (i, t) in self.terrains.iter().enumerate() {
            if !t.ch.is_ascii_graphic() || !ok_token(&t.name) {
                return Err(format!("terrain {i}: bad character or name"));
            }
            if self.terrains[..i].iter().any(|o| o.ch == t.ch || o.name == t.name) {
                return Err(format!("terrain '{}' is defined twice", t.ch));
            }
            if t.defense > 100 {
                return Err(format!("terrain {}: defense is over 100", t.name));
            }
        }
        match self.terrain_index(CITY_CHAR) {
            Some(i) if self.terrains[i].passable() => {}
            _ => return Err(format!("terrain '{CITY_CHAR}' (city) must exist and be passable")),
        }
        if self.units.is_empty() {
            return Err("there must be at least one unit type".into());
        }
        for (i, u) in self.units.iter().enumerate() {
            if !u.code.is_ascii_alphabetic() || !ok_token(&u.name) {
                return Err(format!("unit type {i}: bad code or name"));
            }
            if self.units[..i].iter().any(|o| o.code == u.code || o.name == u.name) {
                return Err(format!("unit type '{}' is defined twice", u.code));
            }
            if u.max_hp == 0 || u.range_min == 0 || u.range_min > u.range_max {
                return Err(format!(
                    "unit type {}: needs max_hp >= 1 and 1 <= range_min <= range_max",
                    u.name
                ));
            }
        }
        let n = self.units.len();
        if self.damage.len() != n || self.damage.iter().any(|row| row.len() != n) {
            return Err(format!("the damage table must be {n} x {n}"));
        }
        if let Some(code) = self.start_units.iter().find(|&&code| self.unit_index(code).is_none()) {
            return Err(format!("start_units: no unit type '{code}'"));
        }
        Ok(())
    }

    /// `TERRAIN`, `UNITTYPE`, `DAMAGE`, and `RULE` lines (SPEC §6.2), plus a
    /// `START` line when `with_start` is set. Bots never see `START`; it only
    /// appears in replay headers.
    pub fn lines(&self, with_start: bool) -> Vec<String> {
        let mut out = Vec::new();
        for t in &self.terrains {
            out.push(format!(
                "TERRAIN {} {} {} {} {} {}",
                t.ch, t.name, t.move_cost, t.defense, t.vision_bonus, t.conceal
            ));
        }
        for u in &self.units {
            out.push(format!(
                "UNITTYPE {} {} {} {} {} {} {} {} {}",
                u.code,
                u.name,
                u.cost,
                u.max_hp,
                u.mv,
                u.vision,
                u.range_min,
                u.range_max,
                u.flags()
            ));
        }
        for (u, row) in self.units.iter().zip(&self.damage) {
            let cells: Vec<String> = row.iter().map(|d| d.to_string()).collect();
            out.push(format!("DAMAGE {} {}", u.code, cells.join(" ")));
        }
        for name in Constants::NAMES {
            out.push(format!("RULE {} {}", name, self.c.get(name).unwrap_or(0)));
        }
        if with_start {
            let codes: Vec<String> = self.start_units.iter().map(|c| c.to_string()).collect();
            out.push(format!("START {}", codes.join(" ")));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_valid() {
        Rules::default().validate().unwrap();
    }

    #[test]
    fn config_keys() {
        let mut r = Rules::default();
        r.set("round_limit", "50").unwrap();
        r.set("unit.K.cost", "300").unwrap();
        r.set("unit.archer.range_max", "3").unwrap();
        r.set("damage.K.P", "30").unwrap();
        r.set("terrain.hills.defense", "50").unwrap();
        r.set("terrain.plains.conceal", "2").unwrap();
        r.set("start_units", "I, K").unwrap();
        assert_eq!(r.c.round_limit, 50);
        assert_eq!(r.units[2].cost, 300);
        assert_eq!(r.units[3].range_max, 3);
        assert_eq!(r.damage[2][1], 30);
        assert_eq!(r.terrains[2].defense, 50);
        assert_eq!(r.terrains[0].conceal, 2);
        assert_eq!(r.start_units, vec!['I', 'K']);
        assert!(r.set("nope", "1").is_err());
        assert!(r.set("round_limit", "-1").is_err());
        r.validate().unwrap();
    }
}
