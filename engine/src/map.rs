//! The map grid, and parsing of setup lines (SPEC §3.1, §6.2).

use crate::hex::Hex;
use crate::rules::{Rules, Terrain, UnitType, CITY_CHAR, SITE_CHAR};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CityInfo {
    pub hex: Hex,
    /// Owner at the start of the game: 0 for neutral, or 1 or 2.
    pub owner: u8,
}

/// A resource site (STRATEGIC.md §3): it grows `output` wheat a round and
/// piles up at most `cap`, for whichever player's wagon comes to load it.
/// Sites have no owner; `SITE` lines keep an owner field, always 0.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SiteInfo {
    pub hex: Hex,
    pub output: u32,
    pub cap: u32,
}

/// A `width × height` grid in the odd-r layout: column `i` of row `r` holds
/// the hex `(i − floor(r/2), r)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Map {
    pub width: i32,
    pub height: i32,
    /// Terrain index into `Rules::terrains` for each cell, row by row.
    pub tiles: Vec<u8>,
    /// Cities in id order.
    pub cities: Vec<CityInfo>,
    /// Resource sites in id order. Only maps for the strategic ruleset have any.
    pub sites: Vec<SiteInfo>,
}

impl Map {
    pub fn len(&self) -> usize {
        self.tiles.len()
    }

    pub fn is_empty(&self) -> bool {
        self.tiles.is_empty()
    }

    pub fn contains(&self, h: Hex) -> bool {
        // r is checked first, so r / 2 below is a floor division.
        h.r >= 0 && h.r < self.height && {
            let col = h.q + h.r / 2;
            col >= 0 && col < self.width
        }
    }

    /// Storage index of a hex on the map.
    pub fn index(&self, h: Hex) -> usize {
        (h.r * self.width + h.q + h.r / 2) as usize
    }

    pub fn hex(&self, index: usize) -> Hex {
        let r = index as i32 / self.width;
        let col = index as i32 % self.width;
        Hex::new(col - r / 2, r)
    }

    /// The hex's image under the 180° rotation `(i, r) → (W−1−i, H−1−r)`.
    pub fn mirror(&self, h: Hex) -> Hex {
        self.hex(self.len() - 1 - self.index(h))
    }

    pub fn terrain_at(&self, h: Hex) -> usize {
        self.tiles[self.index(h)] as usize
    }

    /// `MAP`, `ROW`, `CITY`, and `SITE` lines.
    pub fn lines(&self, rules: &Rules) -> Vec<String> {
        let mut out = vec![format!("MAP {} {}", self.width, self.height)];
        for row in self.tiles.chunks(self.width as usize) {
            let s: String = row.iter().map(|&t| rules.terrains[t as usize].ch).collect();
            out.push(format!("ROW {s}"));
        }
        for (id, c) in self.cities.iter().enumerate() {
            out.push(format!("CITY {} {} {} {}", id, c.hex.q, c.hex.r, c.owner));
        }
        for (id, s) in self.sites.iter().enumerate() {
            out.push(format!("SITE {} {} {} 0 {} {}", id, s.hex.q, s.hex.r, s.output, s.cap));
        }
        out
    }

    /// Checks the map against the rules: every city sits on a city hex and
    /// every city hex has a city, and the same for sites.
    pub fn validate(&self, rules: &Rules) -> Result<(), String> {
        if self.width < 1 || self.height < 2 || self.height % 2 != 0 {
            return Err("the map needs width >= 1 and an even height".into());
        }
        if self.tiles.len() != (self.width * self.height) as usize {
            return Err("the tile count doesn't match the map size".into());
        }
        if self.tiles.iter().any(|&t| t as usize >= rules.terrains.len()) {
            return Err("the map uses an unknown terrain".into());
        }
        let city_t = rules.terrain_index(CITY_CHAR).ok_or("no city terrain")? as u8;
        let mut has_city = vec![false; self.len()];
        for (id, c) in self.cities.iter().enumerate() {
            if !self.contains(c.hex) {
                return Err(format!("city {id} at {} is off the map", c.hex));
            }
            let i = self.index(c.hex);
            if self.tiles[i] != city_t {
                return Err(format!("city {id} at {} isn't on a '{CITY_CHAR}' hex", c.hex));
            }
            if has_city[i] {
                return Err(format!("two cities at {}", c.hex));
            }
            if c.owner > 2 {
                return Err(format!("city {id} has owner {}; owners are 0, 1, or 2", c.owner));
            }
            has_city[i] = true;
        }
        if let Some(i) = (0..self.len()).find(|&i| self.tiles[i] == city_t && !has_city[i]) {
            return Err(format!("the '{CITY_CHAR}' hex at {} has no CITY line", self.hex(i)));
        }
        let site_t = rules.terrain_index(SITE_CHAR).map(|t| t as u8);
        let mut has_site = vec![false; self.len()];
        for (id, s) in self.sites.iter().enumerate() {
            if !self.contains(s.hex) {
                return Err(format!("site {id} at {} is off the map", s.hex));
            }
            let i = self.index(s.hex);
            if Some(self.tiles[i]) != site_t {
                return Err(format!("site {id} at {} isn't on a '{SITE_CHAR}' hex", s.hex));
            }
            if has_site[i] {
                return Err(format!("two sites at {}", s.hex));
            }
            has_site[i] = true;
        }
        if let Some(i) = (0..self.len()).find(|&i| Some(self.tiles[i]) == site_t && !has_site[i]) {
            return Err(format!("the '{SITE_CHAR}' hex at {} has no SITE line", self.hex(i)));
        }
        if !self.sites.is_empty() && !rules.supply() {
            return Err("the map has sites, which need supply = 1".into());
        }
        for p in 1..=2 {
            if !self.cities.iter().any(|c| c.owner == p) {
                return Err(format!("player {p} owns no city"));
            }
        }
        Ok(())
    }
}

fn int<T: std::str::FromStr>(tok: Option<&&str>, what: &str) -> Result<T, String> {
    let tok = tok.ok_or_else(|| format!("missing {what}"))?;
    tok.parse().map_err(|_| format!("bad {what} '{tok}'"))
}

fn char_tok(tok: Option<&&str>, what: &str) -> Result<char, String> {
    let tok = tok.ok_or_else(|| format!("missing {what}"))?;
    let mut chars = tok.chars();
    match (chars.next(), chars.next()) {
        (Some(c), None) => Ok(c),
        _ => Err(format!("bad {what} '{tok}'")),
    }
}

/// Parses setup lines: the `MAP`, `ROW`, and `CITY` lines of a map, plus any
/// `TERRAIN`, `UNITTYPE`, `DAMAGE`, `RULE`, and `START` lines, starting from
/// `rules`. A `TERRAIN` or `UNITTYPE` line replaces that whole table. This
/// reads hand-made map files, replay headers, and init messages alike.
pub fn parse_setup<'a, I>(lines: I, mut rules: Rules) -> Result<(Rules, Map), String>
where
    I: IntoIterator<Item = &'a str>,
{
    let mut size: Option<(i32, i32)> = None;
    let mut rows: Vec<String> = Vec::new();
    let mut cities: Vec<(Hex, u8)> = Vec::new();
    let mut sites: Vec<SiteInfo> = Vec::new();
    let mut terrains: Vec<Terrain> = Vec::new();
    let mut units: Vec<UnitType> = Vec::new();
    let mut damage: Vec<(char, Vec<u32>)> = Vec::new();
    let mut start: Option<Vec<char>> = None;
    let mut rule_lines: Vec<(String, String)> = Vec::new();

    for (n, raw) in lines.into_iter().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let t: Vec<&str> = line.split_whitespace().collect();
        let parsed: Result<(), String> = (|| {
            match t[0] {
                "BITBATH" | "PLAYER" | "INIT" => {}
                "MAP" => size = Some((int(t.get(1), "width")?, int(t.get(2), "height")?)),
                "ROW" => rows.push(t.get(1).ok_or("missing row")?.to_string()),
                "CITY" => {
                    let id: usize = int(t.get(1), "city id")?;
                    if id != cities.len() {
                        return Err(format!("expected city id {}", cities.len()));
                    }
                    let hex = Hex::new(int(t.get(2), "q")?, int(t.get(3), "r")?);
                    cities.push((hex, int(t.get(4), "owner")?));
                }
                "SITE" => {
                    let id: usize = int(t.get(1), "site id")?;
                    if id != sites.len() {
                        return Err(format!("expected site id {}", sites.len()));
                    }
                    let owner: u8 = int(t.get(4), "owner")?;
                    if owner != 0 {
                        return Err(format!(
                            "site {id} has owner {owner}, but sites have no owners: it must be 0"
                        ));
                    }
                    sites.push(SiteInfo {
                        hex: Hex::new(int(t.get(2), "q")?, int(t.get(3), "r")?),
                        output: int(t.get(5), "output")?,
                        cap: int(t.get(6), "cap")?,
                    });
                }
                "TERRAIN" => {
                    let ch = char_tok(t.get(1), "terrain character")?;
                    terrains.push(Terrain::from_fields(ch, &t[2..])?);
                }
                "UNITTYPE" => {
                    let code = char_tok(t.get(1), "unit code")?;
                    units.push(UnitType::from_fields(code, &t[2..])?);
                }
                "DAMAGE" => {
                    let code = char_tok(t.get(1), "unit code")?;
                    let row = t[2..].iter().map(|s| s.parse::<u32>()).collect::<Result<Vec<_>, _>>();
                    damage.push((code, row.map_err(|_| "bad damage value")?));
                }
                "RULE" => {
                    let key = t.get(1).ok_or("missing rule name")?;
                    let value = t.get(2).ok_or("missing rule value")?;
                    rule_lines.push((key.to_string(), value.to_string()));
                }
                "START" => {
                    let codes = t[1..]
                        .iter()
                        .map(|s| char_tok(Some(s), "unit code"))
                        .collect::<Result<_, _>>()?;
                    start = Some(codes);
                }
                other => return Err(format!("unknown keyword '{other}'")),
            }
            Ok(())
        })();
        parsed.map_err(|e| format!("line {}: {e}: {line}", n + 1))?;
    }

    if !terrains.is_empty() {
        rules.terrains = terrains;
    }
    if !units.is_empty() {
        let n = units.len();
        rules.units = units;
        rules.damage = vec![vec![0; n]; n];
    }
    let n = rules.units.len();
    for (code, row) in damage {
        let a = rules
            .unit_index(code)
            .ok_or_else(|| format!("DAMAGE: no unit type '{code}'"))?;
        if row.len() != n {
            return Err(format!("DAMAGE {code}: expected {n} values"));
        }
        rules.damage[a] = row;
    }
    for (key, value) in rule_lines {
        rules.set(&key, &value)?;
    }
    if let Some(codes) = start {
        rules.start_units = codes;
    }

    let (width, height) = size.ok_or("missing MAP line")?;
    if rows.len() != height.max(0) as usize {
        return Err(format!("expected {height} ROW lines, got {}", rows.len()));
    }
    let mut tiles = Vec::with_capacity((width.max(0) * height.max(0)) as usize);
    for (r, row) in rows.iter().enumerate() {
        if row.chars().count() != width.max(0) as usize {
            return Err(format!("row {r} should be {width} characters long"));
        }
        for ch in row.chars() {
            let t = rules
                .terrain_index(ch)
                .ok_or_else(|| format!("row {r}: unknown terrain '{ch}'"))?;
            tiles.push(t as u8);
        }
    }
    let map = Map {
        width,
        height,
        tiles,
        cities: cities.into_iter().map(|(hex, owner)| CityInfo { hex, owner }).collect(),
        sites,
    };
    rules.c.map_width = width as u32;
    rules.c.map_height = height as u32;
    rules.c.neutral_cities = map.cities.iter().filter(|c| c.owner == 0).count() as u32;
    if rules.supply() {
        rules.c.sites = map.sites.len() as u32;
    }
    rules.validate()?;
    map.validate(&rules)?;
    Ok((rules, map))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SMALL: &str = "MAP 4 4\nROW C...\nROW .^..\nROW ..f.\nROW ...C\nCITY 0 0 0 1\nCITY 1 2 3 2\n";

    #[test]
    fn storage_round_trip() {
        let (_, map) = parse_setup(SMALL.lines(), Rules::default()).unwrap();
        for i in 0..map.len() {
            assert_eq!(map.index(map.hex(i)), i);
        }
        assert_eq!(map.hex(4 * 2), Hex::new(-1, 2));
        assert!(map.contains(Hex::new(-1, 2)));
        assert!(!map.contains(Hex::new(-2, 2)));
        assert!(!map.contains(Hex::new(0, -1)));
        assert_eq!(map.mirror(Hex::new(0, 0)), Hex::new(2, 3));
    }

    #[test]
    fn setup_round_trip() {
        let (rules, map) = parse_setup(SMALL.lines(), Rules::default()).unwrap();
        let mut lines = map.lines(&rules);
        lines.extend(rules.lines(true));
        let (rules2, map2) = parse_setup(lines.iter().map(|s| s.as_str()), Rules::default()).unwrap();
        assert_eq!(rules, rules2);
        assert_eq!(map, map2);
    }

    #[test]
    fn added_unit_types_and_terrains_round_trip() {
        let mut rules = Rules::default();
        rules.set("terrain.s", "swamp 3 10 0 1").unwrap();
        rules.set("unit.W", "wagon 100 100 4 2 1 1 -").unwrap();
        rules.set("damage.I.W", "70").unwrap();
        let map_lines = SMALL.replace("ROW ..f.", "ROW ..fs");
        let (rules, map) = parse_setup(map_lines.lines(), rules).unwrap();
        let mut lines = map.lines(&rules);
        lines.extend(rules.lines(true));
        let (rules2, map2) = parse_setup(lines.iter().map(|s| s.as_str()), Rules::default()).unwrap();
        assert_eq!(rules, rules2);
        assert_eq!(map, map2);
    }

    #[test]
    fn sites_round_trip_and_need_supply() {
        let mut rules = Rules::default();
        rules.set("terrain.R", "site 1 0 0 0").unwrap();
        let map_lines = SMALL.replace("ROW ..f.", "ROW ..fR") + "SITE 0 2 2 0 50 1000\n";
        assert!(parse_setup(map_lines.lines(), rules.clone()).is_err());
        rules.set("supply", "1").unwrap();
        let (rules, map) = parse_setup(map_lines.lines(), rules).unwrap();
        assert_eq!(rules.c.sites, 1);
        assert_eq!(map.sites[0].hex, Hex::new(2, 2));
        let mut lines = map.lines(&rules);
        lines.extend(rules.lines(true));
        assert!(lines.contains(&"SITE 0 2 2 0 50 1000".to_string()));
        assert!(lines.contains(&"RULE ration_max 60".to_string()));
        let (rules2, map2) = parse_setup(lines.iter().map(|s| s.as_str()), Rules::default()).unwrap();
        assert_eq!(rules, rules2);
        assert_eq!(map, map2);
        // An R hex needs a SITE line.
        let missing = map_lines.replace("SITE 0 2 2 0 50 1000\n", "");
        assert!(parse_setup(missing.lines(), rules.clone()).is_err());
        // Sites have no owners.
        let owned = map_lines.replace("SITE 0 2 2 0", "SITE 0 2 2 1");
        assert!(parse_setup(owned.lines(), rules.clone()).is_err());
    }

    #[test]
    fn the_default_game_sends_no_supply_rules() {
        let lines = Rules::default().lines(false);
        assert!(!lines
            .iter()
            .any(|l| l.starts_with("RULE supply") || l.starts_with("RULE ration")));
    }

    #[test]
    fn old_terrain_lines_have_no_concealment() {
        // Replays recorded before concealment have five-field TERRAIN lines.
        let old = format!(
            "{SMALL}TERRAIN . plains 1 0 0\nTERRAIN f forest 2 25 0\nTERRAIN C city 1 30 0\nTERRAIN ^ mountains 0 0 0\n"
        );
        let (rules, _) = parse_setup(old.lines(), Rules::default()).unwrap();
        assert_eq!(rules.terrains.len(), 4);
        assert!(rules.terrains.iter().all(|t| t.conceal == 0));
    }

    #[test]
    fn rejects_bad_maps() {
        let bad = SMALL.replace("CITY 1 2 3 2\n", "");
        assert!(parse_setup(bad.lines(), Rules::default()).is_err());
        let bad = SMALL.replace("ROW ..f.", "ROW ..f");
        assert!(parse_setup(bad.lines(), Rules::default()).is_err());
    }
}
