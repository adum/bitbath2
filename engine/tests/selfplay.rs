//! Plays games of random orders, valid and invalid, checking that the engine
//! never panics, keeps its bookkeeping consistent, and replays identically.

use bitbath_engine::mapgen::generate;
use bitbath_engine::rng::SplitMix64;
use bitbath_engine::{Game, Hex, Rules, Unit};

/// Whether the city at `hex` is a port.
fn port(g: &Game, hex: Hex) -> bool {
    hex.neighbors()
        .into_iter()
        .any(|h| g.map.contains(h) && g.rules.terrains[g.map.terrain_at(h)].naval_move_cost > 0)
}

/// Builds a ship at a port, boards a land unit onto one of its player's
/// transports next to it, lands a unit aboard one, or attacks an enemy
/// transport next to a galley, so naval games try them all often.
fn naval_order(g: &Game, rng: &mut SplitMix64) -> Option<String> {
    let pick = |n: usize, rng: &mut SplitMix64| rng.below(n.max(1) as u64) as usize;
    let mine: Vec<&Unit> = g.units.iter().filter(|u| u.owner == g.player).collect();
    let roll = rng.below(4);
    if roll == 3 {
        let fights: Vec<(u32, u32)> = mine
            .iter()
            .filter(|u| g.rules.units[u.ty].naval && !g.rules.units[u.ty].transport)
            .flat_map(|a| {
                g.units
                    .iter()
                    .filter(|d| d.owner != g.player && g.rules.units[d.ty].transport && d.hex.distance(a.hex) == 1)
                    .map(|d| (a.id, d.id))
            })
            .collect();
        let (a, d) = fights.get(pick(fights.len(), rng))?;
        return Some(format!("A {a} {d}"));
    }
    if roll == 0 {
        let ports: Vec<u32> = g
            .cities
            .iter()
            .filter(|c| c.owner == g.player && port(g, c.hex))
            .map(|c| c.id)
            .collect();
        let city = ports.get(pick(ports.len(), rng))?;
        Some(format!("B {city} {}", ['T', 'G'][rng.below(2) as usize]))
    } else if roll == 1 {
        let transports: Vec<&&Unit> = mine.iter().filter(|u| g.rules.units[u.ty].transport).collect();
        let t = transports.get(pick(transports.len(), rng))?;
        let walkers: Vec<(u32, usize)> = mine
            .iter()
            .filter(|u| !g.rules.units[u.ty].naval && u.carrier == 0)
            .filter_map(|u| (0..6).find(|&d| u.hex.step(d) == t.hex).map(|d| (u.id, d)))
            .collect();
        let (id, d) = walkers.get(pick(walkers.len(), rng))?;
        Some(format!("M {id} {d}"))
    } else {
        let riders: Vec<u32> = mine.iter().filter(|u| u.carrier != 0).map(|u| u.id).collect();
        let id = riders.get(pick(riders.len(), rng))?;
        Some(format!("M {id} {}", rng.below(6)))
    }
}

fn random_order(g: &Game, rng: &mut SplitMix64) -> String {
    let naval = g.rules.naval();
    if naval && rng.below(4) == 0 {
        if let Some(order) = naval_order(g, rng) {
            return order;
        }
    }
    let mine: Vec<u32> = g.units.iter().filter(|u| u.owner == g.player).map(|u| u.id).collect();
    let any_unit = |rng: &mut SplitMix64| -> u32 {
        if !mine.is_empty() && rng.below(5) > 0 {
            mine[rng.below(mine.len() as u64) as usize]
        } else {
            rng.below(g.next_unit_id as u64 + 2) as u32
        }
    };
    match rng.below(24) {
        0..=9 => {
            let len = 1 + rng.below(5);
            let dirs: String = (0..len).map(|_| char::from(b'0' + rng.below(6) as u8)).collect();
            format!("M {} {dirs}", any_unit(rng))
        }
        10..=13 => {
            let target = g.units[rng.below(g.units.len() as u64) as usize].id;
            format!("A {} {target}", any_unit(rng))
        }
        14..=15 => format!("C {}", any_unit(rng)),
        16..=18 => {
            let codes = ['I', 'P', 'K', 'A', 'S', 'W', 'Z', 'T', 'G'];
            let n = if naval { 9 } else { 7 };
            format!(
                "B {} {}",
                rng.below(g.cities.len() as u64 + 1),
                codes[rng.below(n) as usize]
            )
        }
        19..=20 => format!("L {}", any_unit(rng)),
        21..=22 => format!("U {} {}", any_unit(rng), rng.below(400)),
        _ => [
            "",
            "M",
            "M 1 7",
            "A x y",
            "B 0",
            "C 1 2",
            "Q",
            "M 99999999999 0",
            "L",
            "U 1 x",
        ][rng.below(10) as usize]
            .to_string(),
    }
}

fn snapshot(g: &Game) -> String {
    format!(
        "{:?} {:?} {:?} {:?} {} {:?}",
        g.units, g.cities, g.sites, g.gold, g.round, g.outcome
    )
}

/// How often units boarded, landed, and sank with their transport.
#[derive(Default, Debug)]
struct Voyages {
    boarded: u32,
    landed: u32,
    sunk: u32,
}

/// Plays a game of random orders on each seed's map, after `prepare` sets
/// the game up.
fn play_random_games(rules: Rules, seeds: impl IntoIterator<Item = u64>, prepare: fn(&mut Game)) -> Voyages {
    let mut voyages = Voyages::default();
    let mut finished = 0;
    for seed in seeds {
        let map = generate(&rules, seed).unwrap();
        let mut g = Game::new(rules.clone(), map.clone());
        prepare(&mut g);
        let mut rng = SplitMix64::new(seed);
        let mut script: Vec<(u8, Vec<String>)> = Vec::new();
        'game: for _ in 0..rules.c.round_limit {
            for player in [1u8, 2] {
                let before: Vec<(u32, u32)> = g.units.iter().map(|u| (u.id, u.carrier)).collect();
                g.begin_turn(player);
                // A transport that starved to death sank with everything aboard.
                voyages.sunk += before
                    .iter()
                    .filter(|&&(id, c)| c != 0 && g.unit(id).is_none() && g.unit(c).is_none())
                    .count() as u32;
                g.check_invariants()
                    .unwrap_or_else(|e| panic!("seed {seed}, after upkeep: {e}"));
                let mut orders = Vec::new();
                for _ in 0..30 {
                    let order = random_order(&g, &mut rng);
                    let before: Vec<(u32, u32)> = g.units.iter().map(|u| (u.id, u.carrier)).collect();
                    let reply = g.apply(&order);
                    for (id, carrier) in before {
                        match (carrier, g.unit(id).map(|u| u.carrier)) {
                            (0, Some(c)) if c != 0 => voyages.boarded += 1,
                            (c, Some(0)) if c != 0 => voyages.landed += 1,
                            (c, None) if c != 0 => voyages.sunk += 1,
                            _ => {}
                        }
                    }
                    let last = reply.last().expect("every order gets a reply");
                    assert!(
                        last.starts_with("OK") || last.starts_with("PART") || last.starts_with("ERR"),
                        "'{order}' -> {reply:?}"
                    );
                    assert!(reply[..reply.len() - 1].iter().all(|l| l.starts_with("SEE ")));
                    g.check_invariants()
                        .unwrap_or_else(|e| panic!("seed {seed}, after '{order}': {e}"));
                    orders.push(order);
                    if g.outcome.is_some() {
                        break;
                    }
                }
                script.push((player, orders));
                g.end_turn();
                if g.outcome.is_some() {
                    break 'game;
                }
            }
        }
        if g.outcome.is_some() {
            finished += 1;
        }

        // Re-simulating the same orders must give exactly the same game.
        let mut again = Game::new(rules.clone(), map);
        prepare(&mut again);
        for (player, orders) in &script {
            again.begin_turn(*player);
            for order in orders {
                again.apply(order);
            }
            again.end_turn();
        }
        assert_eq!(snapshot(&g), snapshot(&again), "seed {seed}");
    }
    assert!(
        finished > 0,
        "no game finished, so the round limit or elimination never triggered"
    );
    voyages
}

fn small_rules() -> Rules {
    let mut rules = Rules::default();
    rules.c.map_width = 16;
    rules.c.map_height = 12;
    rules.c.neutral_cities = 4;
    rules.c.round_limit = 60;
    rules
}

#[test]
fn random_orders_keep_the_game_consistent() {
    play_random_games(small_rules(), 0..8, |_| {});
}

#[test]
fn random_orders_keep_a_supply_game_consistent() {
    play_random_games(supply_rules(), 0..8, |_| {});
}

/// The supply rules on the small map, with sites and wagons.
fn supply_rules() -> Rules {
    let mut rules = small_rules();
    for (key, value) in [
        ("supply", "1"),
        ("sites", "4"),
        ("ration_max", "6"),
        ("terrain.R", "site 1 0 0 0"),
        ("unit.W", "wagon 100 100 4 2 1 1 carry"),
        ("damage.I.W", "70"),
        ("damage.K.W", "90"),
        ("start_units", "I, I, S, W"),
    ] {
        rules.set(key, value).unwrap();
    }
    rules
}

#[test]
fn random_orders_keep_a_naval_game_consistent() {
    let mut rules = supply_rules();
    for (key, value) in [
        ("map_width", "24"),
        ("map_height", "18"),
        ("neutral_cities", "6"),
        ("water_pct", "40"),
        ("passengers_max", "2"),
        ("terrain.water.naval_move_cost", "1"),
        ("unit.T", "transport 150 100 6 2 1 1 naval,transport"),
        ("unit.G", "galley 200 100 5 3 1 1 naval,zoc"),
        ("damage.A.T", "40"),
        ("damage.A.G", "40"),
        ("damage.G.I", "25"),
        ("damage.G.W", "50"),
        ("damage.G.T", "80"),
        ("damage.G.G", "55"),
        ("start_bread", "2000"),
    ] {
        rules.set(key, value).unwrap();
    }
    // Homes are never ports, so each player starts with a mirrored pair of
    // neutral ports too, with bread, and ships get built from the start.
    let v = play_random_games(rules, 0..8, |g| {
        let pair = (2..g.cities.len())
            .step_by(2)
            .find(|&i| port(g, g.cities[i].hex))
            .unwrap();
        for (ci, player) in [(pair, 1), (pair + 1, 2)] {
            g.cities[ci].owner = player;
            g.cities[ci].bread = 2000;
        }
    });
    assert!(v.boarded > 0 && v.landed > 0 && v.sunk > 0, "{v:?}");
}
