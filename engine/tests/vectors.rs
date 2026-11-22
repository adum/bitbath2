//! Runs the golden test vectors in tests/vectors (SPEC §9).

use bitbath_engine::{damage, parse_setup, Game, Hex, Map, Rules};
use serde_json::Value;

fn load(name: &str) -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../tests/vectors")
        .join(name);
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).unwrap()
}

fn int(v: &Value) -> i64 {
    v.as_i64().unwrap_or_else(|| panic!("expected an integer, got {v}"))
}

fn hex(v: &Value) -> Hex {
    Hex::new(int(&v[0]) as i32, int(&v[1]) as i32)
}

fn list(v: &Value) -> &Vec<Value> {
    v.as_array().unwrap_or_else(|| panic!("expected an array, got {v}"))
}

fn strings(v: &Value) -> Vec<String> {
    list(v).iter().map(|s| s.as_str().unwrap().to_string()).collect()
}

fn ch(v: &Value) -> char {
    v.as_str().unwrap().chars().next().unwrap()
}

fn blank_map(w: i32, h: i32) -> Map {
    Map {
        width: w,
        height: h,
        tiles: vec![0; (w * h) as usize],
        cities: Vec::new(),
        sites: Vec::new(),
    }
}

#[test]
fn hex_vectors() {
    let v = load("hex.json");
    for (dir, offset) in list(&v["directions"]).iter().enumerate() {
        assert_eq!(Hex::new(0, 0).step(dir), hex(offset));
    }
    for case in list(&v["distance"]) {
        assert_eq!(
            hex(&case["a"]).distance(hex(&case["b"])) as i64,
            int(&case["distance"]),
            "{case}"
        );
    }
    for case in list(&v["neighbors"]) {
        let want: Vec<Hex> = list(&case["neighbors"]).iter().map(hex).collect();
        assert_eq!(hex(&case["hex"]).neighbors().to_vec(), want);
    }
    for case in list(&v["storage"]) {
        let (w, h) = (int(&case["width"]) as i32, int(&case["height"]) as i32);
        let map = blank_map(w, h);
        let at = hex(&case["hex"]);
        let on_map = case["on_map"].as_bool().unwrap();
        assert_eq!(map.contains(at), on_map, "{case}");
        if on_map {
            assert_eq!(
                map.index(at) as i64,
                int(&case["row"]) * w as i64 + int(&case["col"]),
                "{case}"
            );
        }
    }
    for case in list(&v["mirror"]) {
        let map = blank_map(int(&case["width"]) as i32, int(&case["height"]) as i32);
        assert_eq!(map.mirror(hex(&case["hex"])), hex(&case["mirror"]), "{case}");
    }
}

#[test]
fn damage_vectors() {
    let v = load("damage.json");
    let rules = Rules::default();
    let unit = |s: &Value| rules.unit_index(ch(s)).unwrap();
    let defense = |s: &Value| rules.terrains[rules.terrain_index(ch(s)).unwrap()].defense;
    for case in list(&v["damage"]) {
        let dealt = damage(
            &rules,
            unit(&case["attacker"]),
            int(&case["attacker_hp"]) as u32,
            unit(&case["defender"]),
            defense(&case["terrain"]),
        );
        assert_eq!(dealt as i64, int(&case["damage"]), "{case}");
    }
}

/// Plays each combat vector as a real attack order: the attacker on (1,1)
/// and the defender 1 or 2 hexes east of it, on the given terrains, with
/// any given fortification levels.
#[test]
fn combat_vectors() {
    let v = load("damage.json");
    for case in list(&v["combat"]) {
        let dist = int(&case["distance"]) as usize;
        let mut row1: Vec<char> = "......".chars().collect();
        row1[1] = ch(&case["attacker_terrain"]);
        row1[1 + dist] = ch(&case["defender_terrain"]);
        let row1: String = row1.into_iter().collect();
        let mut lines = vec![
            "MAP 6 4".to_string(),
            "ROW C.....".into(),
            format!("ROW {row1}"),
            "ROW ......".into(),
            "ROW .....C".into(),
        ];
        lines.push("CITY 0 0 0 1".into());
        let mut next_city = 1;
        for (i, c) in row1.chars().enumerate() {
            if c == 'C' {
                lines.push(format!("CITY {next_city} {i} 1 0"));
                next_city += 1;
            }
        }
        lines.push(format!("CITY {next_city} 4 3 2"));
        let (rules, map) = parse_setup(lines.iter().map(|s| s.as_str()), Rules::default()).unwrap();
        let mut g = Game::empty(rules, map);
        let att = g.rules.unit_index(ch(&case["attacker"])).unwrap();
        let def = g.rules.unit_index(ch(&case["defender"])).unwrap();
        g.place(1, 1, att, Hex::new(1, 1), int(&case["attacker_hp"]) as u32)
            .unwrap();
        g.place(
            2,
            2,
            def,
            Hex::new(1 + dist as i32, 1),
            int(&case["defender_hp"]) as u32,
        )
        .unwrap();
        // Fortification levels, given only for fortified fights (units 1 and 2, in id order).
        g.units[0].fort = case.get("attacker_fort").map_or(0, int) as u32;
        g.units[1].fort = case.get("defender_fort").map_or(0, int) as u32;
        g.begin_turn(1);
        let want = format!(
            "OK {} {}",
            int(&case["defender_hp_after"]),
            int(&case["attacker_hp_after"])
        );
        assert_eq!(g.apply("A 1 2"), vec![want], "{case}");
        g.check_invariants().unwrap();
    }
}

/// Places each case's unit alone on the vector map and compares what player
/// 1 sees. The map has no cities, so nothing else adds vision.
#[test]
fn vision_vectors() {
    let v = load("vision.json");
    let rules = Rules::default();
    let rows = strings(&v["map"]);
    let tiles = rows
        .iter()
        .flat_map(|row| row.chars().map(|c| rules.terrain_index(c).unwrap() as u8))
        .collect();
    let map = Map {
        width: rows[0].len() as i32,
        height: rows.len() as i32,
        tiles,
        cities: Vec::new(),
        sites: Vec::new(),
    };
    for case in list(&v["cases"]) {
        let mut g = Game::empty(rules.clone(), map.clone());
        let ty = rules.unit_index(ch(&case["unit"])).unwrap();
        g.place(1, 1, ty, hex(&case["hex"]), 100).unwrap();
        let vis = g.vision(1);
        let seen: Vec<Hex> = (0..map.len()).filter(|&i| vis[i]).map(|i| map.hex(i)).collect();
        let want: Vec<Hex> = list(&case["visible"]).iter().map(hex).collect();
        assert_eq!(seen, want, "{case}");
    }
}

#[test]
fn order_vectors() {
    let v = load("orders.json");
    let map_lines = strings(&v["map"]);
    for sc in list(&v["scenarios"]) {
        let name = sc["name"].as_str().unwrap();
        let mut rules = Rules::default();
        if let Some(overrides) = sc.get("rules").and_then(|r| r.as_object()) {
            for (key, value) in overrides {
                rules.set(key, value.as_str().unwrap()).unwrap();
            }
        }
        let (rules, map) = parse_setup(map_lines.iter().map(|s| s.as_str()), rules).unwrap();
        let mut g = Game::empty(rules, map);
        if let Some(cities) = sc.get("cities") {
            for c in strings(cities) {
                let t: Vec<u32> = c.split_whitespace().map(|x| x.parse().unwrap()).collect();
                let city = &mut g.cities[t[0] as usize];
                city.owner = t[1] as u8;
                city.capturer = t[2] as u8;
                city.progress = t[3];
            }
        }
        for u in strings(&sc["units"]) {
            let t: Vec<&str> = u.split_whitespace().collect();
            let ty = g.rules.unit_index(t[2].chars().next().unwrap()).unwrap();
            let at = Hex::new(t[3].parse().unwrap(), t[4].parse().unwrap());
            g.place(
                t[0].parse().unwrap(),
                t[1].parse().unwrap(),
                ty,
                at,
                t[5].parse().unwrap(),
            )
            .unwrap_or_else(|e| panic!("{name}: {e}"));
            // The unit just placed has the highest id, so it's last.
            g.units.last_mut().unwrap().fort = t.get(6).map_or(0, |f| f.parse().unwrap());
        }
        g.gold = [0, int(&sc["gold"][0]) as u32, int(&sc["gold"][1]) as u32];
        play_scenario(sc, g);
    }
}

/// Begins the scenario's turn, so upkeep runs, then checks its turn
/// message, the reply to each order, the outcome, and the other player's
/// next turn message.
fn play_scenario(sc: &Value, mut g: Game) {
    let name = sc["name"].as_str().unwrap();
    let player = int(&sc["player"]) as u8;
    let round = int(&sc["round"]) as u32;
    g.round = if player == 1 { round - 1 } else { round };
    g.begin_turn(player);
    if let Some(msg) = sc.get("message") {
        assert_eq!(g.turn_message(0, 0), strings(msg), "{name}: turn message");
    }
    for step in list(&sc["steps"]) {
        let order = step["order"].as_str().unwrap();
        let reply = g.apply(order);
        let want = strings(&step["reply"]);
        assert_eq!(
            reply.len(),
            want.len(),
            "{name}: '{order}' replied {reply:?}, expected {want:?}"
        );
        for (got, expected) in reply.iter().zip(&want) {
            if expected.starts_with("ERR ") {
                assert!(
                    got.starts_with(&format!("{expected} ")),
                    "{name}: '{order}' replied '{got}', expected '{expected}'"
                );
            } else {
                assert_eq!(got, expected, "{name}: '{order}'");
            }
        }
        g.check_invariants()
            .unwrap_or_else(|e| panic!("{name}: after '{order}': {e}"));
    }
    if let Some(o) = sc.get("outcome") {
        let outcome = g
            .outcome
            .clone()
            .unwrap_or_else(|| panic!("{name}: the game should be over"));
        assert_eq!(outcome.winner as i64, int(&o["winner"]), "{name}");
        assert_eq!(outcome.reason, o["reason"].as_str().unwrap(), "{name}");
    } else {
        assert!(g.outcome.is_none(), "{name}: the game ended unexpectedly");
    }
    if let Some(msg) = sc.get("next_message") {
        g.end_turn();
        g.begin_turn(3 - player);
        assert_eq!(g.turn_message(0, 0), strings(msg), "{name}: next turn message");
    }
}

/// The supply rules of the strategic ruleset (STRATEGIC.md §4, §5). Cities,
/// sites, and units are given as in `CITY`, `SITE`, and `UNIT` lines, and a
/// unit may end with what it did on its last turn: `M` moved, `A` attacked.
#[test]
fn supply_vectors() {
    let v = load("supply.json");
    let map_lines = strings(&v["map"]);
    for sc in list(&v["scenarios"]) {
        let name = sc["name"].as_str().unwrap();
        let mut rules = Rules::default();
        for lines in [v.get("rules"), sc.get("rules")].into_iter().flatten() {
            for line in strings(lines) {
                let (key, value) = line.split_once('=').unwrap();
                rules.set(key.trim(), value.trim()).unwrap();
            }
        }
        let (rules, map) = parse_setup(map_lines.iter().map(|s| s.as_str()), rules).unwrap();
        let mut g = Game::empty(rules, map);
        for c in strings(&sc["cities"]) {
            let t: Vec<u32> = c.split_whitespace().map(|x| x.parse().unwrap()).collect();
            let city = &mut g.cities[t[0] as usize];
            (city.owner, city.capturer, city.progress, city.bread) = (t[1] as u8, t[2] as u8, t[3], t[4]);
        }
        for s in strings(&sc["sites"]) {
            let t: Vec<u32> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
            let site = &mut g.sites[t[0] as usize];
            (site.owner, site.capturer, site.progress, site.wheat) = (t[1] as u8, t[2] as u8, t[3], t[4]);
        }
        for u in strings(&sc["units"]) {
            let t: Vec<&str> = u.split_whitespace().collect();
            let num = |i: usize| t[i].parse::<u32>().unwrap();
            let ty = g.rules.unit_index(t[2].chars().next().unwrap()).unwrap();
            let at = Hex::new(t[3].parse().unwrap(), t[4].parse().unwrap());
            g.place(num(0), num(1) as u8, ty, at, num(5))
                .unwrap_or_else(|e| panic!("{name}: {e}"));
            assert_eq!(num(7), 0, "{name}: no unit can be aboard a transport yet");
            let last = t.get(11).copied().unwrap_or("-");
            let unit = g.units.last_mut().unwrap();
            (unit.fort, unit.wheat, unit.bread, unit.rations) = (num(6), num(8), num(9), num(10));
            (unit.moved, unit.attacked) = (last.contains('M'), last.contains('A'));
        }
        play_scenario(sc, g);
    }
}
